#!/usr/bin/env python3
# Dump all GATT services/characteristics/descriptors of a BLE device,
# read readable values and briefly listen on notify/indicate characteristics.
import asyncio
import sys
from bleak import BleakScanner, BleakClient

TARGET_NAME = sys.argv[1] if len(sys.argv) > 1 else "CS500"
LISTEN_SECONDS = 5.0


async def main():
    print(f"Scanning for '{TARGET_NAME}'...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name is not None and TARGET_NAME in d.name, timeout=30.0)
    if device is None:
        print("Device not found.")
        return

    async with BleakClient(device, timeout=60.0) as client:
        print(f"Connected to {device.name} [{device.address}], MTU={client.mtu_size}\n")

        notifiable = []
        for service in client.services:
            print(f"[Service] {service.uuid}  handle={service.handle}  ({service.description})")
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"  [Char] {char.uuid}  handle={char.handle}  props=[{props}]  ({char.description})")
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char)
                        print(f"         value={bytes(value)!r}  hex={value.hex()}")
                    except Exception as e:
                        print(f"         read failed: {e}")
                for desc in char.descriptors:
                    try:
                        value = await client.read_gatt_descriptor(desc.handle)
                        print(f"    [Desc] {desc.uuid}  handle={desc.handle}  value={value.hex()}")
                    except Exception as e:
                        print(f"    [Desc] {desc.uuid}  handle={desc.handle}  read failed: {e}")
                if "notify" in char.properties or "indicate" in char.properties:
                    notifiable.append(char)
            print()

        # Subscribe to everything notifiable and see what arrives.
        def make_handler(uuid):
            def handler(_, data):
                print(f"  <notify {uuid}> {bytes(data)!r}  hex={data.hex()}")
            return handler

        for char in notifiable:
            try:
                await client.start_notify(char, make_handler(char.uuid))
                print(f"Subscribed to {char.uuid}")
            except Exception as e:
                print(f"Subscribe to {char.uuid} failed: {e}")

        print(f"\nListening {LISTEN_SECONDS}s for unsolicited notifications...")
        await asyncio.sleep(LISTEN_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
