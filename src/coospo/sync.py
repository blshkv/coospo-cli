"""Shared by `coospo sync` and `coospo watch`: download new activities, then run the post-sync hook."""

import asyncio
import os
import re
import shlex
import shutil
import sys

from .fit import activity_time, fit_complete


def extract_fit_filenames(file_path):
    '''filelist.txt has one "YYYYMMDDhhmmss.fit <size>" entry per line.'''
    pattern = re.compile(r'\d{14}\.fit')
    with open(file_path, 'r') as file:
        return sorted(m.group(0) for line in file if (m := pattern.search(line)))


async def fetch_filelist(dev, work_dir):
    '''Download filelist.txt and return the activity names on the device, or None on failure.'''
    if not await dev.fetch_file('filelist.txt', work_dir):
        return None
    return extract_fit_filenames(os.path.join(work_dir, 'filelist.txt'))


async def init_job(dev, index, staging, since):
    '''Mark the activities on the device (from before `since`, "YYYYMMDD") as handled.'''
    serial = (await dev.read_device_info())[2]
    names = await fetch_filelist(dev, os.path.join(staging, serial))
    if names is None:
        return 1
    old = [n for n in names if since is None or n[:8] < since]
    index.skip(serial, old)
    index.set_baselined(serial)
    print(f"{len(old)} of {len(names)} activities on {serial} marked as handled, "
          f"{len(names) - len(old)} left to sync.")
    return 0


async def sync_job(dev, index, staging, keep_dir, hook):
    '''Download the activities the index doesn't know yet; return the number of failures.'''
    serial = (await dev.read_device_info())[2]
    if hook and not index.is_baselined(serial):
        print(f"Error: {serial} has no baseline; run 'coospo init' first, or all its activities "
              "would be passed to the post-sync command.", file=sys.stderr)
        return 1
    work = os.path.join(staging, serial)
    names = await fetch_filelist(dev, work)
    if names is None:
        return 1
    known = index.known(serial)
    new = [n for n in names if n not in known]
    print(f"{len(names)} activities on the device, {len(new)} new.")
    failed = pending = 0
    for i, name in enumerate(new, 1):
        print(f"[{i}/{len(new)}] {name}")
        if not await dev.fetch_file(name, work):
            failed += 1
            continue
        path = os.path.join(work, name)
        with open(path, 'rb') as file:
            complete = fit_complete(file.read())
        if not complete:
            # Probably still being recorded; it is fetched again on the next run.
            print(f"Skipping {name}: not a complete FIT file yet.")
            os.remove(path)
            pending += 1
            continue
        staged = not keep_dir
        if keep_dir:
            os.makedirs(keep_dir, exist_ok=True)
            path = shutil.move(path, os.path.join(keep_dir, name))
        index.add_downloaded(serial, name, path, staged)
    print(f"Done: {len(new) - failed - pending} downloaded, {pending} not complete yet, {failed} failed.")
    return failed


async def run_hook(command, path, serial, name, timeout):
    '''Run `command path` with the activity described in the environment; True if it exits 0.'''
    env = dict(os.environ, COOSPO_FILE=path, COOSPO_SERIAL=serial)
    if start := activity_time(name):
        env['COOSPO_ACTIVITY_TIME'] = start.isoformat()
    proc = await asyncio.create_subprocess_exec(*shlex.split(command), path, env=env)
    try:
        return await asyncio.wait_for(proc.wait(), timeout) == 0
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        print(f"Error: post-sync command timed out on {name}", file=sys.stderr)
        return False


async def run_hooks(index, command, timeout=300.0):
    '''Run the post-sync command on every downloaded file it has not succeeded on; return failures.'''
    failed = 0
    for serial, name, path, staged in index.hooks_due():
        if not os.path.exists(path):
            print(f"Error: {path} is gone; cannot run the post-sync command on it.", file=sys.stderr)
            ok = False
        else:
            print(f"Post-sync: {name}")
            ok = await run_hook(command, path, serial, name, timeout)
        if ok:
            index.hook_done(serial, name)
            if staged:
                os.remove(path)
        else:
            index.hook_failed(serial, name)
            failed += 1
            print(f"Post-sync command failed on {name}; will retry.", file=sys.stderr)
    return failed
