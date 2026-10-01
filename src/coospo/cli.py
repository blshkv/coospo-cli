"""Command-line interface: coospo info | sync | get."""

import argparse
import asyncio
import os
import re
import sys
import warnings

from bleak import BleakClient

from . import __version__
from .device import SUPPORTED_MODELS, CoospoDevice

FIT_FOLDER = "fit_files"   # default output directory


def extract_fit_filenames(file_path):
    '''filelist.txt has one "YYYYMMDDhhmmss.fit <size>" entry per line.'''
    pattern = re.compile(r'\d{14}\.fit')
    with open(file_path, 'r') as file:
        return sorted(m.group(0) for line in file if (m := pattern.search(line)))


async def cmd_info(dev, args):
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


async def cmd_sync(dev, args):
    if not await dev.fetch_file('filelist.txt', args.output):
        return 1
    names = extract_fit_filenames(os.path.join(args.output, 'filelist.txt'))
    new = [n for n in names if not os.path.exists(os.path.join(args.output, n))]
    print(f"{len(names)} activities on the device, {len(new)} new.")
    failed = 0
    for i, name in enumerate(new, 1):
        print(f"[{i}/{len(new)}] {name}")
        if not await dev.fetch_file(name, args.output):
            failed += 1
    print(f"Done: {len(new) - failed} downloaded, {len(names) - len(new)} already present, {failed} failed.")
    return 1 if failed else 0


async def cmd_get(dev, args):
    failed = 0
    for name in args.files:
        if not await dev.fetch_file(name, args.output):
            failed += 1
    return 1 if failed else 0


async def main(args):
    dev = CoospoDevice()
    target = args.address or args.name or ' or '.join(SUPPORTED_MODELS)
    print(f"Searching for {target}...")
    device = await dev.discover(args.name, args.address, args.scan_timeout)
    if device is None:
        print(f"Error: {target} not found. Make sure the device is on and not connected to the "
              "phone app.", file=sys.stderr)
        return 1

    try:
        async with BleakClient(device, timeout=60.0) as client:
            print(f"Connected to {device.name or device.address}")
            await dev.start(client)
            return await args.func(dev, args)
    except asyncio.TimeoutError:
        print("Error: no response from the device.", file=sys.stderr)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
    return 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='coospo',
        description='Command-line tool for COOSPO CS500/CS600 bike computers over Bluetooth LE.',
        epilog='''examples:
  coospo info                            show device status
  coospo sync                            download new activities into fit_files/
  coospo sync -o ~/rides                 ... into another directory
  coospo get filelist.txt Setting.json   download specific files
  coospo -n CS500-1234567 info           pick one of several devices

The device must not be connected to the phone app at the same time.''',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-V', '--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-n', '--name', metavar='NAME',
                        help='connect to the device whose Bluetooth name contains NAME '
                             '(default: the first one whose name starts with CS500 or CS600)')
    parser.add_argument('-a', '--address', metavar='ADDR',
                        help='connect to the device with this Bluetooth address instead')
    parser.add_argument('-t', '--scan-timeout', type=float, default=30.0, metavar='SEC',
                        help='how long to search for the device (default: %(default)s)')
    commands = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)

    p = commands.add_parser(
        'info', help='show model, serial number, firmware, battery and memory usage',
        description='Show model, serial number, hardware and firmware versions, battery level '
                    'and memory usage.')
    p.set_defaults(func=cmd_info)

    p = commands.add_parser(
        'sync', help='download new activities (.fit files)',
        description='Download filelist.txt and every activity (.fit) that is not already in the '
                    'output directory. Nothing is deleted from the device.')
    p.add_argument('-o', '--output', default=FIT_FOLDER, metavar='DIR',
                   help='output directory (default: %(default)s)')
    p.set_defaults(func=cmd_sync)

    p = commands.add_parser(
        'get', help='download files by name',
        description='Download files from the device by name, e.g. filelist.txt, Setting.json or '
                    'an activity such as 20260101080000.fit. Existing files are overwritten.')
    p.add_argument('files', nargs='+', metavar='FILE', help='name of a file on the device')
    p.add_argument('-o', '--output', default=FIT_FOLDER, metavar='DIR',
                   help='output directory (default: %(default)s)')
    p.set_defaults(func=cmd_get)

    return parser.parse_args(argv)


def run():
    """Entry point of the coospo command."""
    # bleak on BlueZ warns about the MTU on every connection; it is harmless here.
    warnings.filterwarnings('ignore', message='Using default MTU value')
    try:
        sys.exit(asyncio.run(main(parse_args())))
    except KeyboardInterrupt:
        sys.exit(130)
