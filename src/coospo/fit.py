"""Minimal FIT helpers: completeness check and the start time encoded in activity file names."""

import datetime

_CRC_TABLE = (0x0000, 0xcc01, 0xd801, 0x1400, 0xf001, 0x3c00, 0x2800, 0xe401,
              0xa001, 0x6c00, 0x7800, 0xb401, 0x5000, 0x9c01, 0x8801, 0x4400)


def fit_crc(data):
    crc = 0
    for byte in data:
        for nibble in (byte & 0xf, byte >> 4):
            crc = (crc >> 4) ^ _CRC_TABLE[crc & 0xf] ^ _CRC_TABLE[nibble]
    return crc


def fit_complete(data):
    '''True if data is a whole FIT file: header, declared data size and trailing CRC all agree.'''
    if len(data) < 16 or data[8:12] != b'.FIT':
        return False
    header_size = data[0]
    data_size = int.from_bytes(data[4:8], 'little')
    if len(data) != header_size + data_size + 2:
        return False
    return fit_crc(data[:-2]) == int.from_bytes(data[-2:], 'little')


def activity_time(name):
    '''Start time (naive, device local time) from a "YYYYMMDDhhmmss.fit" name, or None.'''
    try:
        return datetime.datetime.strptime(name[:14], '%Y%m%d%H%M%S')
    except ValueError:
        return None
