"""`coospo watch`: sync whenever the bike computer comes into range."""

import asyncio
import time

from bleak import BleakScanner

from .config import staging_dir
from .device import device_matcher, run_session
from .sync import run_hooks, sync_job

TICK = 60.0           # seconds between post-sync retries while no device is in range
RETRY_AFTER = 60.0    # seconds before trying a device again after a failed sync


async def wait_for_device(match, next_allowed, timeout):
    '''Scan until a matching device that may be synced again is seen; None after timeout seconds.'''
    found = asyncio.get_running_loop().create_future()

    def on_advertisement(device, advertisement):
        if (not found.done() and match(device, advertisement)
                and time.monotonic() >= next_allowed.get(device.address, 0)):
            found.set_result(device)

    # The scanner is closed again before connecting; BlueZ is unreliable doing both at once.
    async with BleakScanner(detection_callback=on_advertisement):
        try:
            return await asyncio.wait_for(found, timeout)
        except asyncio.TimeoutError:
            return None


async def watch(args, index, keep_dir):
    match = device_matcher(args.name, args.address)
    next_allowed = {}
    print(f"Watching for {args.address or args.name or 'CS500/CS600'}; "
          f"syncing at most every {args.interval:.0f} s per device.", flush=True)
    while True:
        device = await wait_for_device(match, next_allowed, TICK)
        if device is not None:
            print(f"Found {device.name or device.address}", flush=True)
            ok = await run_session(device, sync_job, index, staging_dir(), keep_dir, args.post_sync) == 0
            next_allowed[device.address] = time.monotonic() + (args.interval if ok else min(args.interval, RETRY_AFTER))
        if args.post_sync:
            await run_hooks(index, args.post_sync)
