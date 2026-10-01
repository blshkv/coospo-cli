import os
import tempfile
import unittest

from coospo.cli import extract_fit_filenames
from coospo.device import CoospoDevice
from coospo.protocol import build_frame, crc16_ccitt_false, parse_frame, pb_decode, zigzag

# Example frames from PROTOCOL.md (made-up values, valid lengths and CRCs).
DEVICE_INFO_REQ = '7e 00 00 07 00 01 84 bd 7f'
RESPONSE_ACK = '7e 20 00 05 1a ff 7f'
DEVICE_INFO_RSP = ('7e 10 00 2c 00 01 08 02 10 03 1a 05 56 31 2e 34 31 22 06 56 31 2e 35 2e 30'
                   ' 2a 07 31 32 33 34 35 36 37 32 04 56 31 2e 30 38 c8 01 51 f9 7f')
FETCH_FILELIST = '7e 02 00 15 00 29 0a 0c 66 69 6c 65 6c 69 73 74 2e 74 78 74 6f ca 7f'
FILE_HEADER = '7e 80 00 18 00 2b 0a 0c 66 69 6c 65 6c 69 73 74 2e 74 78 74 10 88 2d a5 f8 7f'
ESCAPED = '7e 05 00 0c 00 29 0a 03 61 7d 01 62 0c dc 7f'


def frame(hex_string):
    return bytes.fromhex(hex_string)


class FrameTest(unittest.TestCase):
    def test_crc(self):
        self.assertEqual(crc16_ccitt_false(b'123456789'), 0x29b1)  # CRC-16/CCITT-FALSE check value

    def test_build_request(self):
        self.assertEqual(build_frame(0x00, 0x0001), frame(DEVICE_INFO_REQ))

    def test_build_ack(self):
        self.assertEqual(build_frame(0x20), frame(RESPONSE_ACK))

    def test_build_with_payload(self):
        self.assertEqual(build_frame(0x02, 0x0029, b'\x0a\x0cfilelist.txt'), frame(FETCH_FILELIST))

    def test_escaping(self):
        self.assertEqual(build_frame(0x05, 0x0029, b'\x0a\x03a}b'), frame(ESCAPED))
        self.assertEqual(parse_frame(frame(ESCAPED)), (0x05, 0x0029, b'\x0a\x03a}b'))

    def test_round_trip(self):
        for hex_string in (DEVICE_INFO_REQ, RESPONSE_ACK, DEVICE_INFO_RSP, FETCH_FILELIST,
                           FILE_HEADER, ESCAPED):
            with self.subTest(hex_string):
                raw = frame(hex_string)
                self.assertEqual(build_frame(*parse_frame(raw)), raw)

    def test_parse_ack(self):
        self.assertEqual(parse_frame(frame(RESPONSE_ACK)), (0x20, None, b''))

    def test_bad_crc(self):
        raw = bytearray(frame(DEVICE_INFO_REQ))
        raw[-2] ^= 0x01
        with self.assertRaisesRegex(ValueError, 'bad crc'):
            parse_frame(bytes(raw))

    def test_bad_length(self):
        raw = bytearray(frame(DEVICE_INFO_REQ))
        raw[3] += 1
        with self.assertRaisesRegex(ValueError, 'bad length'):
            parse_frame(bytes(raw))


class PayloadTest(unittest.TestCase):
    def test_device_info(self):
        _, cmd, payload = parse_frame(frame(DEVICE_INFO_RSP))
        info = pb_decode(payload)
        self.assertEqual(cmd, 0x0001)
        self.assertEqual((info[3], info[4], info[5]), ([b'V1.41'], [b'V1.5.0'], [b'1234567']))
        self.assertEqual(info[7], [200])

    def test_file_header(self):
        _, cmd, payload = parse_frame(frame(FILE_HEADER))
        header = pb_decode(payload)
        self.assertEqual(cmd, 0x002b)
        self.assertEqual(header[1], [b'filelist.txt'])
        self.assertEqual(zigzag(header[2][0]), 2884)

    def test_zigzag(self):
        self.assertEqual([zigzag(n) for n in (0, 1, 2, 3, 72)], [0, -1, 1, -2, 36])


def data_packet(index, data):
    packet = index.to_bytes(4, 'big') + data
    return packet + bytes([sum(packet) & 0xff])


class DataPacketTest(unittest.TestCase):
    def setUp(self):
        self.dev = CoospoDevice()

    def test_in_order(self):
        self.dev.data_handler(None, data_packet(0, b'hello '))
        self.dev.data_handler(None, data_packet(1, b'world'))
        self.assertIsNone(self.dev.data_error)
        self.assertEqual(self.dev.data, b'hello world')

    def test_bad_checksum(self):
        packet = bytearray(data_packet(0, b'hello'))
        packet[-1] ^= 0x01
        self.dev.data_handler(None, bytes(packet))
        self.assertIn('checksum', self.dev.data_error)

    def test_missing_packet(self):
        self.dev.data_handler(None, data_packet(0, b'hello'))
        self.dev.data_handler(None, data_packet(2, b'world'))
        self.assertIn('unexpected packet', self.dev.data_error)


class FileListTest(unittest.TestCase):
    def test_extract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'filelist.txt')
            with open(path, 'w', newline='') as f:
                f.write('20260102080000.fit 1000\r\n20260101080000.fit 123456\r\n')
            self.assertEqual(extract_fit_filenames(path),
                             ['20260101080000.fit', '20260102080000.fit'])


if __name__ == '__main__':
    unittest.main()
