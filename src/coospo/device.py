"""BLE session with a COOSPO CS500/CS600: requests, commands and file download."""

import asyncio
import datetime
import os
import struct

from bleak import BleakScanner

from .protocol import (
    BATTERY_LEVEL_UUID, CMD_DEVICE_INFO, CMD_FETCH_FILE, CMD_FILE_CONFIRM, CMD_FILE_DONE,
    CMD_FILE_HEADER, CMD_SET_TIME, CMD_STATUS, CMD_STORAGE, FDA1_UUID, FDA2_UUID, FDA3_UUID,
    FDA4_UUID, MODEL_NUMBER_UUID, T_CMD, T_CMD_ACK, T_EVT, T_EVT_ACK, T_REQ, T_RSP, T_RSP_ACK,
    build_frame, parse_frame, pb_decode, pb_varint, zigzag,
)

SUPPORTED_MODELS = ("CS500", "CS600")  # advertised as "<model>-<serial>"
TIMEOUT = 10.0             # seconds of silence before giving up


class CoospoDevice:
    def __init__(self):
        self.client = None
        self.seq = 0
        self.rx_buf = {FDA1_UUID: bytearray(), FDA2_UUID: bytearray(), FDA3_UUID: bytearray()}
        self.responses = asyncio.Queue()  # (ctl, cmd, payload) from fda1
        self.cmd_acks = asyncio.Queue()   # ctl from fda2
        self.events = asyncio.Queue()     # (cmd, payload) from fda3
        # **File**
        self.data = bytearray()
        self.next_idx = 0
        self.data_error = None

    def next_seq(self):
        seq = self.seq
        self.seq = (self.seq + 1) & 0x0f
        return seq

    async def write(self, uuid, value):
        await self.client.write_gatt_char(uuid, value, response=False)

    def create_frame_handler(self, uuid):
        async def frame_handler(sender, data):
            buf = self.rx_buf[uuid]
            buf += data
            while (start := buf.find(0x7e)) >= 0 and (end := buf.find(0x7f, start)) >= 0:
                raw = bytes(buf[start:end + 1])
                del buf[:end + 1]
                try:
                    ctl, cmd, payload = parse_frame(raw)
                except ValueError as e:
                    print(f'Error: {e}')
                    continue
                kind = ctl & 0xf0
                if kind == T_RSP:
                    await self.write(FDA1_UUID, build_frame(T_RSP_ACK | (ctl & 0x0f)))
                    self.responses.put_nowait((ctl, cmd, payload))
                elif kind == T_CMD_ACK:
                    self.cmd_acks.put_nowait(ctl)
                elif kind == T_EVT:
                    await self.write(FDA3_UUID, build_frame(T_EVT_ACK | (ctl & 0x0f)))
                    self.events.put_nowait((cmd, payload))
                else:
                    print(f'Unexpected frame: {raw.hex()}')

        return frame_handler

    def data_handler(self, sender, data):
        idx = int.from_bytes(data[:4], 'big')
        if self.data_error:
            return
        if sum(data[:-1]) & 0xff != data[-1]:
            self.data_error = f'checksum error in packet {idx}'
        elif idx != self.next_idx:
            self.data_error = f'unexpected packet: {self.next_idx} -> {idx}'
        else:
            self.data += data[4:-1]
            self.next_idx += 1

    async def request(self, cmd, payload=b''):
        '''Send a request on fda1 and return the payload of the matching response.'''
        seq = self.next_seq()
        await self.write(FDA1_UUID, build_frame(T_REQ | seq, cmd, payload))
        while True:
            ctl, rsp_cmd, rsp_payload = await asyncio.wait_for(self.responses.get(), TIMEOUT)
            if ctl & 0x0f == seq and rsp_cmd == cmd:
                return rsp_payload

    async def command(self, cmd, payload=b'', uuid=FDA2_UUID):
        '''Send a command (on fda2 unless given) and wait for the device to ack it on fda2.'''
        seq = self.next_seq()
        await self.write(uuid, build_frame(T_CMD | seq, cmd, payload))
        while await asyncio.wait_for(self.cmd_acks.get(), TIMEOUT) & 0x0f != seq:
            pass

    async def wait_event(self, cmd):
        while True:
            event_cmd, payload = await asyncio.wait_for(self.events.get(), TIMEOUT)
            if event_cmd == cmd:
                return payload
            print(f'Unexpected event: 0x{event_cmd:04x} {payload.hex()}')

    async def discover(self, name=None, address=None, timeout=30.0):
        if address:
            return await BleakScanner.find_device_by_address(address, timeout=timeout)

        def match(d, ad):
            device_name = d.name or ad.local_name
            if not device_name:
                return False
            return name in device_name if name else device_name.startswith(SUPPORTED_MODELS)

        return await BleakScanner.find_device_by_filter(match, timeout=timeout)

    async def start(self, client):
        self.client = client
        for uuid in (FDA1_UUID, FDA2_UUID, FDA3_UUID):
            await client.start_notify(uuid, self.create_frame_handler(uuid))
        await client.start_notify(FDA4_UUID, self.data_handler)

    async def read_model(self):
        return (await self.client.read_gatt_char(MODEL_NUMBER_UUID)).decode().strip()

    async def read_device_info(self):
        info = pb_decode(await self.request(CMD_DEVICE_INFO))
        hw, fw, serial = (info.get(n, [b'?'])[0].decode() for n in (3, 4, 5))
        return hw, fw, serial

    async def read_battery(self):
        return (await self.client.read_gatt_char(BATTERY_LEVEL_UUID))[0]

    async def read_storage(self):
        '''Return (used, total) in 512-byte blocks.'''
        info = pb_decode(await self.request(CMD_STORAGE))
        free, total = info[1][0], info[2][0]
        return total - free, total

    async def time_set(self):
        now = datetime.datetime.now().astimezone()
        tz_hours = now.utcoffset().total_seconds() / 3600
        payload = (b'\x0d' + int(now.timestamp()).to_bytes(4, 'little')
                   + b'\x15' + struct.pack('<f', tz_hours))
        await self.command(CMD_SET_TIME, payload, FDA1_UUID)

    async def fetch_file(self, filename, out_dir):
        '''Download a file into out_dir; return True on success.'''
        await self.request(CMD_STATUS)
        while not self.events.empty():
            self.events.get_nowait()
        self.data = bytearray()
        self.next_idx = 0
        self.data_error = None

        name = filename.encode('utf-8')
        rsp = pb_decode(await self.request(CMD_FETCH_FILE, b'\x0a' + pb_varint(len(name)) + name))
        if rsp.get(1) != [1]:
            print(f"Error: the device refused to send {filename} (no such file?)")
            return False

        header = pb_decode(await self.wait_event(CMD_FILE_HEADER))
        data_size = zigzag(header[2][0])

        # Wait for FILE_DONE while data keeps flowing on fda4.
        while True:
            last_idx = self.next_idx
            try:
                await self.wait_event(CMD_FILE_DONE)
                break
            except asyncio.TimeoutError:
                if self.next_idx == last_idx:
                    print(f"Error: transfer of {filename} stalled at packet {self.next_idx}.")
                    return False

        if self.data_error:
            print(f"Error: {self.data_error} in {filename}")
            return False
        if len(self.data) != data_size:
            print(f"Error: {filename}: received {len(self.data)} bytes, expected {data_size}")
            return False
        await self.command(CMD_FILE_CONFIRM, b'\x08\x01')

        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, os.path.basename(filename))
        # Write to a temporary file first so an interrupted run never leaves a truncated file
        # that a later sync would skip as already present.
        with open(path + '.part', 'wb') as file:
            file.write(self.data)
        os.replace(path + '.part', path)
        print(f"Saved {path} ({data_size} bytes)")
        return True
