import asyncio
import os
import stat
import tempfile
import types
import unittest

from coospo.config import load_config
from coospo.device import device_matcher
from coospo.fit import activity_time, fit_complete, fit_crc
from coospo.index import Index
from coospo.sync import init_job, run_hooks, sync_job


def make_fit(payload=b'\x01\x02\x03\x04'):
    header = bytes([14, 0x20, 0, 0]) + len(payload).to_bytes(4, 'little') + b'.FIT' + b'\0\0'
    body = header + payload
    return body + fit_crc(body).to_bytes(2, 'little')


class FakeDevice:
    '''Serves files from a dict the way CoospoDevice.fetch_file does.'''
    def __init__(self, files, serial='1234567'):
        self.files = files
        self.serial = serial
        self.fetched = []

    async def read_device_info(self):
        return 'V1', 'V2', self.serial

    async def fetch_file(self, name, out_dir):
        self.fetched.append(name)
        if name == 'filelist.txt':
            data = ''.join(f'{n} {len(d)}\r\n' for n, d in self.files.items() if n.endswith('.fit')).encode()
        else:
            data = self.files[name]
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, name), 'wb') as file:
            file.write(data)
        return True


def run(coro):
    return asyncio.run(coro)


class FitTest(unittest.TestCase):
    def test_complete(self):
        self.assertTrue(fit_complete(make_fit()))

    def test_truncated_or_corrupt(self):
        data = make_fit()
        self.assertFalse(fit_complete(data[:-3]))
        self.assertFalse(fit_complete(data[:-1] + b'\xff'))
        self.assertFalse(fit_complete(b''))

    def test_activity_time(self):
        self.assertEqual(activity_time('20260101080000.fit').isoformat(), '2026-01-01T08:00:00')
        self.assertIsNone(activity_time('Setting.json'))


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.index = Index(':memory:')

    def test_baseline(self):
        self.assertFalse(self.index.is_baselined('1'))
        self.assertFalse(self.index.any_baselined())
        self.index.set_baselined('1')
        self.assertTrue(self.index.is_baselined('1'))
        self.assertFalse(self.index.is_baselined('2'))

    def test_known_is_per_device(self):
        self.index.skip('1', ['a.fit'])
        self.index.add_downloaded('1', 'b.fit', '/x/b.fit', False)
        self.assertEqual(self.index.known('1'), {'a.fit', 'b.fit'})
        self.assertEqual(self.index.known('2'), set())

    def test_skipped_files_have_no_hook(self):
        self.index.skip('1', ['a.fit'])
        self.assertEqual(self.index.hooks_due(), [])

    def test_hook_backoff(self):
        self.index.add_downloaded('1', 'a.fit', '/x/a.fit', False)
        self.assertEqual(len(self.index.hooks_due(now=1000)), 1)
        self.index.hook_failed('1', 'a.fit', now=1000)
        self.assertEqual(self.index.hooks_due(now=1059), [])
        self.assertEqual(len(self.index.hooks_due(now=1060)), 1)
        self.index.hook_failed('1', 'a.fit', now=1060)
        self.assertEqual(self.index.hooks_due(now=1060 + 119), [])
        self.assertEqual(len(self.index.hooks_due(now=1060 + 120)), 1)

    def test_hook_done(self):
        self.index.add_downloaded('1', 'a.fit', '/x/a.fit', False)
        self.index.hook_done('1', 'a.fit')
        self.assertEqual(self.index.hooks_due(), [])


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.staging = os.path.join(self.tmp.name, 'staging')
        self.keep = os.path.join(self.tmp.name, 'keep')
        self.index = Index(':memory:')
        self.files = {'20260101080000.fit': make_fit(b'one'), '20260102080000.fit': make_fit(b'two')}

    def script(self, body):
        path = os.path.join(self.tmp.name, 'hook.sh')
        with open(path, 'w') as file:
            file.write('#!/bin/sh\n' + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_sync_keeps_files_and_does_not_fetch_twice(self):
        dev = FakeDevice(self.files)
        self.assertEqual(run(sync_job(dev, self.index, self.staging, self.keep, None)), 0)
        self.assertEqual(sorted(os.listdir(self.keep)), sorted(self.files))
        dev.fetched.clear()
        self.assertEqual(run(sync_job(dev, self.index, self.staging, self.keep, None)), 0)
        self.assertEqual(dev.fetched, ['filelist.txt'])

    def test_sync_skips_incomplete_file_until_complete(self):
        self.files['20260103080000.fit'] = make_fit()[:-5]
        dev = FakeDevice(self.files)
        run(sync_job(dev, self.index, self.staging, self.keep, None))
        self.assertNotIn('20260103080000.fit', os.listdir(self.keep))
        self.files['20260103080000.fit'] = make_fit()
        run(sync_job(dev, self.index, self.staging, self.keep, None))
        self.assertIn('20260103080000.fit', os.listdir(self.keep))

    def test_hook_requires_baseline(self):
        dev = FakeDevice(self.files)
        self.assertEqual(run(sync_job(dev, self.index, self.staging, None, 'true')), 1)
        self.assertEqual(dev.fetched, [])

    def test_init_then_only_new_files_reach_the_hook(self):
        dev = FakeDevice(self.files)
        self.assertEqual(run(init_job(dev, self.index, self.staging, None)), 0)
        self.files['20260103080000.fit'] = make_fit(b'three')
        self.assertEqual(run(sync_job(dev, self.index, self.staging, None, 'true')), 0)
        self.assertEqual([n for _, n, _, _ in self.index.hooks_due()], ['20260103080000.fit'])

    def test_init_since(self):
        dev = FakeDevice(self.files)
        run(init_job(dev, self.index, self.staging, '20260102'))
        self.assertEqual(self.index.known('1234567'), {'20260101080000.fit'})

    def test_hook_runs_once_and_staged_file_is_deleted(self):
        out = os.path.join(self.tmp.name, 'seen')
        hook = self.script(f'echo "$COOSPO_SERIAL $COOSPO_ACTIVITY_TIME $1" >> {out}\n')
        dev = FakeDevice(self.files)
        run(init_job(dev, self.index, self.staging, '20260102'))
        run(sync_job(dev, self.index, self.staging, None, hook))
        staged = self.index.hooks_due()[0][2]
        self.assertTrue(os.path.exists(staged))
        self.assertEqual(run(run_hooks(self.index, hook)), 0)
        self.assertFalse(os.path.exists(staged))
        self.assertEqual(run(run_hooks(self.index, hook)), 0)
        with open(out) as file:
            self.assertEqual(file.read(), f'1234567 2026-01-02T08:00:00 {staged}\n')

    def test_failed_hook_is_kept_for_retry(self):
        hook = self.script('exit 1\n')
        dev = FakeDevice(self.files)
        run(init_job(dev, self.index, self.staging, '20260102'))
        run(sync_job(dev, self.index, self.staging, self.keep, hook))
        self.assertEqual(run(run_hooks(self.index, hook)), 1)
        self.assertEqual(self.index.hooks_due(), [])  # backing off
        self.assertEqual(len(self.index.hooks_due(now=1e12)), 1)
        self.assertIn('20260102080000.fit', os.listdir(self.keep))  # kept files are never deleted


class MatcherTest(unittest.TestCase):
    def device(self, name, address='AA:BB'):
        return types.SimpleNamespace(name=name, address=address), types.SimpleNamespace(local_name=None)

    def test_default_models(self):
        match = device_matcher()
        self.assertTrue(match(*self.device('CS500-1355265')))
        self.assertTrue(match(*self.device('CS600-1')))
        self.assertFalse(match(*self.device('Phone')))
        self.assertFalse(match(*self.device(None)))

    def test_name_and_address(self):
        self.assertTrue(device_matcher(name='1355')(*self.device('CS500-1355265')))
        self.assertTrue(device_matcher(address='aa:bb')(*self.device('anything')))
        self.assertFalse(device_matcher(address='aa:cc')(*self.device('CS500-1')))


class ConfigTest(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            self.assertEqual(load_config(path, {'interval'}), {})
            with open(path, 'w') as file:
                file.write('interval = 300\npost_sync = "x"\n')
            self.assertEqual(load_config(path, {'interval'}), {'interval': 300})


if __name__ == '__main__':
    unittest.main()
