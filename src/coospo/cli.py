"""Command-line interface: coospo info | sync | init | watch | get."""

import argparse
import asyncio
import os
import signal
import sys
import warnings

from . import __version__
from .config import config_path, data_dir, index_path, load_config, staging_dir
from .device import CoospoDevice, run_session
from .index import Index
from .sync import init_job, run_hooks, sync_job
from .watch import watch

# Settings that can also be given in the config file; None on the command line means "not given".
DEFAULTS = {'name': None, 'address': None, 'scan_timeout': 30.0, 'output': None,
            'post_sync': None, 'interval': 600.0}


async def show_info(dev, args):
    model = await dev.read_model()
    hw, fw, serial = await dev.read_device_info()
    battery = await dev.read_battery()
    used, total = await dev.read_storage()
    print(f"Model:     {model}")
    print(f"Serial:    {serial}")
    print(f"Hardware:  {hw}")
    print(f"Firmware:  {fw}")
    print(f"Battery:   {battery}%")
    print(f"Memory:    {used / 2048:.1f}M/{total / 2048:.1f}M used ({100 * used // total}%)")
    return 0


async def get_files(dev, args):
    failed = 0
    for name in args.files:
        if not await dev.fetch_file(name, os.path.expanduser(args.output or default_output())):
            failed += 1
    return 1 if failed else 0


def default_output():
    return os.path.join(data_dir(), 'fit_files')


def keep_dir(args):
    '''Where downloaded activities stay: the output directory, or nowhere if a post-sync command
    takes them and no output directory was asked for.'''
    return os.path.expanduser(args.output) if args.output else (None if args.post_sync else default_output())


async def with_device(args, func, *func_args):
    '''Find the device, connect and call func(dev, *func_args).'''
    target = args.address or args.name or 'Coospo compatible device'
    print(f"Searching for {target}...")
    device = await CoospoDevice().discover(args.name, args.address, args.scan_timeout)
    if device is None:
        print(f"Error: {target} not found. Make sure the device is on and not connected to the "
              "phone app.", file=sys.stderr)
        return 1
    return await run_session(device, func, *func_args)


async def cmd_info(args):
    return await with_device(args, show_info, args)


async def cmd_get(args):
    return await with_device(args, get_files, args)


async def cmd_init(args):
    index = Index(index_path())
    return await with_device(args, init_job, index, staging_dir(), args.since)


async def cmd_sync(args):
    index = Index(index_path())
    rc = await with_device(args, sync_job, index, staging_dir(), keep_dir(args), args.post_sync)
    # Also when the device was not found: earlier downloads may still wait for the command.
    if args.post_sync and await run_hooks(index, args.post_sync):
        rc = 1
    return rc


async def cmd_watch(args):
    index = Index(index_path())
    if args.post_sync and not index.any_baselined():
        print("Error: no device has a baseline yet; run 'coospo init' first, or all activities "
              "on the device would be passed to the post-sync command.", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(line_buffering=True)  # logs go to a pipe or file under a service manager
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    await watch(args, index, keep_dir(args))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='coospo',
        description='Command-line tool for COOSPO CS500/CS600 bike computers over Bluetooth LE.',
        epilog='''examples:
  coospo info                              show device status
  coospo sync                              download new activities
  coospo sync -o ~/rides                   ... and keep them in another directory
  coospo init                              ignore the activities already on the device
  coospo watch --post-sync ./upload.sh     sync whenever the device is in range, then upload
  coospo get filelist.txt Setting.json     download specific files
  coospo -n CS500-1234567 info             pick one of several devices

Settings can also be put in ~/.config/coospo/config.toml (name, address, scan_timeout, output,
post_sync, interval). The device must not be connected to the phone app at the same time.''',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-V', '--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-c', '--config', metavar='FILE', default=config_path(),
                        help='config file (default: %(default)s)')
    parser.add_argument('-n', '--name', metavar='NAME',
                        help='connect to the device whose Bluetooth name contains NAME '
                             '(default: the first one whose name starts with CS500 or CS600)')
    parser.add_argument('-a', '--address', metavar='ADDR',
                        help='connect to the device with this Bluetooth address instead')
    parser.add_argument('-t', '--scan-timeout', type=float, metavar='SEC',
                        help='how long to search for the device (default: 30)')
    commands = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)

    p = commands.add_parser(
        'info', help='show model, serial number, firmware, battery and memory usage',
        description='Show model, serial number, hardware and firmware versions, battery level '
                    'and memory usage.')
    p.set_defaults(func=cmd_info)

    def add_sync_options(p):
        p.add_argument('-o', '--output', metavar='DIR',
                       help='keep the activities in DIR (default: only with no --post-sync, in '
                            f'{default_output()})')
        p.add_argument('-p', '--post-sync', metavar='CMD',
                       help='run CMD FILE for every new activity, e.g. an uploader; it is retried '
                            'with growing delays until it exits 0, and the downloaded file is '
                            'deleted afterwards unless --output is given')

    p = commands.add_parser(
        'sync', help='download new activities (.fit files)',
        description='Download every activity the index (~/.local/state/coospo/index.db) does not '
                    'know yet, then run the post-sync command on the ones it has not succeeded '
                    'on. Nothing is deleted from the device.')
    add_sync_options(p)
    p.set_defaults(func=cmd_sync)

    p = commands.add_parser(
        'init', help='ignore the activities already on the device',
        description='Record the activities currently on the device as handled, so that sync and '
                    'watch only pass new ones to the post-sync command. Required once per device '
                    'before using --post-sync.')
    p.add_argument('--since', metavar='YYYYMMDD',
                   help='keep activities from this date on as new')
    p.set_defaults(func=cmd_init)

    p = commands.add_parser(
        'watch', help='sync whenever the device is in range',
        description='Run until stopped; whenever the device is seen, sync it like `coospo sync` '
                    'does, at most once per interval.')
    add_sync_options(p)
    p.add_argument('-i', '--interval', type=float, metavar='SEC',
                   help='minimum time between two syncs of the same device (default: 600)')
    p.set_defaults(func=cmd_watch)

    p = commands.add_parser(
        'get', help='download files by name',
        description='Download files from the device by name, e.g. filelist.txt, Setting.json or '
                    'an activity such as 20260101080000.fit. Existing files are overwritten; the '
                    'index is not used.')
    p.add_argument('files', nargs='+', metavar='FILE', help='name of a file on the device')
    p.add_argument('-o', '--output', metavar='DIR',
                   help=f'output directory (default: {default_output()})')
    p.set_defaults(func=cmd_get)

    args = parser.parse_args(argv)
    config = load_config(args.config, DEFAULTS.keys())
    for key, default in DEFAULTS.items():
        if getattr(args, key, False) is None:
            setattr(args, key, config.get(key, default))
    return args


def run():
    """Entry point of the coospo command."""
    # bleak on BlueZ warns about the MTU on every connection; it is harmless here.
    warnings.filterwarnings('ignore', message='Using default MTU value')
    args = parse_args()
    try:
        sys.exit(asyncio.run(args.func(args)))
    except KeyboardInterrupt:
        sys.exit(130)
