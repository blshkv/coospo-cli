"""COOSPO CS500/CS600 BLE protocol: framing, checksums and protobuf helpers.

The protocol below was reverse-engineered from an HCI snoop log of the CoospoRide app.

GATT service 0xFDA0:
  fda1  app requests (0x0_) / device responses (0x1_) / app acks the response (0x2_)
  fda2  app commands (0x4_, SET_TIME is sent on fda1) / device acks (0x6_)
  fda3  device events (0x8_) / app acks the event (0xa_)
  fda4  raw file data

Frame (fda1-fda3): 7e | ctl | len(2, BE) | cmd(2, BE) | protobuf payload | crc16(2, BE) | 7f
  ctl:   high nibble = frame type (see above), low nibble = sequence number (0-15).
  len:   length of the unescaped frame without the 7e/7f delimiters.
  crc16: CRC-16/CCITT-FALSE over ctl..payload.
  0x7d, 0x7e and 0x7f inside the frame are escaped as 7d 01, 7d 02 and 7d 03.
  An ack frame has no cmd and no payload.

Data packet (fda4): index(4, BE) | data | sum8(index + data)

Download of a file:
  fda1  -> STATUS (0x0032)                <- STATUS
  fda1  -> FETCH_FILE (0x0029) {1: name}  <- FETCH_FILE {1: 1}
  fda3  <- FILE_HEADER (0x002b) {1: name, 2: sint size}
  fda4  <- data packets
  fda3  <- FILE_DONE (0x002e)
  fda2  -> FILE_CONFIRM (0x002f) {1: 1}

See PROTOCOL.md for the full description.
"""

FDA1_UUID = "0000fda1-0000-1000-8000-00805f9b34fb"
FDA2_UUID = "0000fda2-0000-1000-8000-00805f9b34fb"
FDA3_UUID = "0000fda3-0000-1000-8000-00805f9b34fb"
FDA4_UUID = "0000fda4-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"  # standard Battery Service
MODEL_NUMBER_UUID = "00002a24-0000-1000-8000-00805f9b34fb"   # standard Device Information

# Frame types (high nibble of ctl).
T_REQ, T_RSP, T_RSP_ACK = 0x00, 0x10, 0x20  # fda1
T_CMD, T_CMD_ACK = 0x40, 0x60               # fda2
T_EVT, T_EVT_ACK = 0x80, 0xa0               # fda3

CMD_DEVICE_INFO = 0x0001   # fda1; {3: hw, 4: fw, 5: serial, ...}
CMD_SET_TIME = 0x0002      # fda1, acked on fda2; {1: fixed32 unix time, 2: float tz hours}
CMD_FETCH_FILE = 0x0029    # fda1; {1: filename} -> {1: 1 on success}
CMD_FILE_HEADER = 0x002b   # fda3 event; {1: filename, 2: sint size}
CMD_FILE_DONE = 0x002e     # fda3 event
CMD_FILE_CONFIRM = 0x002f  # fda2; {1: 1}
CMD_STATUS = 0x0032        # fda1; empty -> empty
CMD_STORAGE = 0x0033       # fda1; {1: free, 2: total} in 512-byte blocks


def crc16_ccitt_false(data):
    crc = 0xffff
    for x in data:
        crc ^= x << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
    return crc


def escape(data):
    out = bytearray()
    for x in data:
        if x in (0x7d, 0x7e, 0x7f):
            out += bytes([0x7d, x - 0x7c])
        else:
            out.append(x)
    return out


def unescape(data):
    out = bytearray()
    it = iter(data)
    for x in it:
        if x == 0x7d:
            x = next(it) + 0x7c
        out.append(x)
    return out


def build_frame(ctl, cmd=None, payload=b''):
    cmd_bytes = b'' if cmd is None else cmd.to_bytes(2, 'big')
    length = 1 + 2 + len(cmd_bytes) + len(payload) + 2
    body = bytes([ctl]) + length.to_bytes(2, 'big') + cmd_bytes + payload
    return b'\x7e' + escape(body + crc16_ccitt_false(body).to_bytes(2, 'big')) + b'\x7f'


def parse_frame(raw):
    '''Return (ctl, cmd, payload) of a 7e..7f frame; cmd is None for an ack.'''
    inner = unescape(raw[1:-1])
    if int.from_bytes(inner[1:3], 'big') != len(inner):
        raise ValueError(f'bad length: {raw.hex()}')
    if crc16_ccitt_false(inner[:-2]) != int.from_bytes(inner[-2:], 'big'):
        raise ValueError(f'bad crc: {raw.hex()}')
    cmd = int.from_bytes(inner[3:5], 'big') if len(inner) > 5 else None
    return inner[0], cmd, bytes(inner[5:-2])


def pb_varint(value):
    out = bytearray()
    while True:
        out.append((value & 0x7f) | (0x80 if value > 0x7f else 0))
        value >>= 7
        if not value:
            return bytes(out)


def pb_decode(buf):
    '''Minimal protobuf decoder: {field_number: [values]}.'''
    fields = {}
    i = 0

    def varint():
        nonlocal i
        value = shift = 0
        while True:
            b = buf[i]
            i += 1
            value |= (b & 0x7f) << shift
            shift += 7
            if not b & 0x80:
                return value

    while i < len(buf):
        key = varint()
        field, wire = key >> 3, key & 0x7
        if wire == 0:
            value = varint()
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            n = varint()
            value, i = buf[i:i + n], i + n
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f'unsupported wire type {wire}')
        fields.setdefault(field, []).append(value)
    return fields


def zigzag(n):
    return (n >> 1) ^ -(n & 1)
