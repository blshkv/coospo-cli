# COOSPO CS500 BLE file-transfer protocol

Reverse-engineered notes for downloading files (FIT activities, `filelist.txt`, `Setting.json`) from a
COOSPO CS500 bike computer over Bluetooth LE, without the CoospoRide app.

- Source: an Android HCI snoop log of CoospoRide syncing with the device, plus live tests with
  [bleak](https://github.com/hbldh/bleak) on Linux/BlueZ.
- Tested device: CS500, hardware V1.41, firmware V1.5.0.
- Reference implementation: the `coospo` package in this repository (`src/coospo/`, Python 3.9+,
  bleak).
- All example frames below are re-encoded with made-up values (serial number, timestamps, file names),
  but they have valid lengths and CRCs.

Status legend: **confirmed** = seen in the capture and exercised by the reference implementation;
**observed** = seen in the capture only; **unknown** = not understood yet.

## 1. GATT layout

The device advertises as `CS500-<serial>`. It does **not** expose the Nordic UART service
(`6e400001-b5a3-f393-e0a9-e50e24dcca9e`) used by XOSS/Cycplus/older COOSPO devices, and it does not
answer their YMODEM-style commands.

| Service | Characteristic | Properties | Use |
|---|---|---|---|
| `0xFDA0` | `fda1` | write, write-no-rsp, notify | requests / responses |
| `0xFDA0` | `fda2` | write, write-no-rsp, notify | commands and their acks |
| `0xFDA0` | `fda3` | write, write-no-rsp, notify | device events and their acks |
| `0xFDA0` | `fda4` | write, write-no-rsp, notify | raw file data (device → app) |
| `0xFD00` | `fd09` / `fd0a` | notify / write-no-rsp | separate protocol, see §7 |
| `0x180A`, `0x180F` | standard | read | Device Information, Battery |

The app enables notifications on `fda1`–`fda4` and `fd09`, then writes all frames with
write-without-response. The protocol works at any ATT MTU; data packets simply get shorter at
smaller MTUs.

### 1.1 Link layer — observed

What the CoospoRide session (Android phone as central) showed below the GATT level:

| Item | Observed |
|---|---|
| ATT MTU | the **device** starts the MTU exchange requesting 200; the phone offers 517, so 200 is used |
| LE features of the device | mask `0x4925`: LE Data Packet Length Extension, LE 2M PHY and LE Coded PHY are supported |
| Data Length Extension | requested by the phone; afterwards max 199 bytes phone → device and 204 bytes device → phone |
| PHY | never changed: no LE Set PHY or PHY Update Complete in the capture, so the link stays on 1M |
| Preferred connection parameters (GAP `0x2A04`) | interval 20–40 ms, latency 0, supervision timeout 4 s |
| Connection parameters used | 18.75 ms at connection; 48.75 ms with latency 5 during the file transfers |

With MTU 200, one data notification is 3 (ATT header) + 197 bytes, which plus the 4-byte L2CAP
header is exactly 204 bytes, so each `fda4` data packet travels in a single link-layer packet.
It's not visible in the log who requested the change to 48.75 ms: the device sent no L2CAP
connection parameter update request.

## 2. Frame format (`fda1`, `fda2`, `fda3`) — confirmed

```
7e | ctl | len (2, BE) | cmd (2, BE) | payload | crc (2, BE) | 7f
```

| Field | Description |
|---|---|
| `7e` / `7f` | start / end delimiters |
| `ctl` | high nibble = frame type (§3), low nibble = sequence number 0–15 |
| `len` | length of the **unescaped** frame **without** the `7e`/`7f` delimiters |
| `cmd` | command id (§4); absent in ack frames |
| `payload` | usually a protobuf message (§5); may be empty |
| `crc` | CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`, no reflection, xorout 0) over `ctl` … end of `payload` |

**Escaping:** inside the frame (between the delimiters, including the CRC), the bytes `7d`, `7e` and
`7f` are sent as `7d 01`, `7d 02` and `7d 03`. `len` and `crc` are computed over the unescaped bytes.
Because `7f` only ever appears as the end delimiter, frames split across notifications can be
reassembled by buffering until `7f`.

**Ack frames** carry only `ctl`, `len = 0005` and the CRC.

Examples:

```
7e 00 00 07 00 01 84 bd 7f                     request, seq 0, cmd 0x0001, no payload
7e 20 00 05 1a ff 7f                           ack of response seq 0
7e 05 00 0c 00 29 0a 03 61 7d 01 62 0c dc 7f   payload "a}b": the 7d byte is escaped as 7d 01
```

## 3. Frame types and channels — confirmed

| Type (`ctl & 0xF0`) | Direction | Characteristic | Meaning |
|---|---|---|---|
| `0x00` | app → device | `fda1` | request |
| `0x10` | device → app | `fda1` | response (same seq as the request) |
| `0x20` | app → device | `fda1` | app acks a response (same seq) |
| `0x40` | app → device | `fda2` (SET_TIME: `fda1`) | command |
| `0x60` | device → app | `fda2` | device acks a command (same seq) |
| `0x80` | device → app | `fda3` | event |
| `0xA0` | app → device | `fda3` | app acks an event (same seq) |

- The app uses **one** sequence counter for request (`0x0_`) and command (`0x4_`) frames. It wraps at
  16 and starts at 0 on each connection.
- The device's event counter (`0x8_`) is independent and was **not** reset on reconnection. Acks
  simply echo whatever seq the device sent.
- The app acks every response and every event immediately.

## 4. Commands

| cmd | Frame | Request payload | Response / ack | Status |
|---|---|---|---|---|
| `0x0001` | request | – | device info, §5.1 | confirmed |
| `0x0002` | command | set time, §5.2 | ack | observed |
| `0x0004` | request | – | `{1:1, 3:1, 4:1, 5:1, 6:1, 9:1, 10:1, 11:1}` (capability flags?) | unknown |
| `0x0029` | request | fetch file `{1: filename}` | `{1: 1}` when accepted | confirmed |
| `0x002b` | event (download), command (upload) | file header `{1: filename, 2: sint64 size}` | – | confirmed for download |
| `0x002c` | command | upload data chunk, §6.2 | ack | observed |
| `0x002e` | event | end of download, no payload | – | confirmed |
| `0x002f` | command | `{1: 1}` = download received OK | ack | confirmed |
| `0x0032` | request | – | empty (status / idle check) | confirmed |
| `0x0033` | request | – | storage `{1: free, 2: total}` in 512-byte blocks, e.g. `{1: 3384, 2: 55168}` = 25.3M/26.9M used (93%) | confirmed |
| `0x0036` | request | – | GNSS/AGPS info, §5.3 | observed |

## 5. Payloads

Payloads are protobuf wire format with no length prefix. Field numbers are listed below; names are
guesses.

### 5.1 Device info (`0x0001` response) — confirmed

| Field | Type | Example | Guess |
|---|---|---|---|
| 1 | varint | 2 | ? |
| 2 | varint | 3 | ? |
| 3 | string | `V1.41` | hardware revision |
| 4 | string | `V1.5.0` | firmware revision |
| 5 | string | `1234567` | serial number (also in the advertised name) |
| 6 | string | `V1.0` | ? |
| 7 | varint | 200 | ATT MTU? (matches the MTU the device requests, §1.1) |

```
7e 10 00 2c 00 01 08 02 10 03 1a 05 56 31 2e 34 31 22 06 56 31 2e 35 2e 30
   2a 07 31 32 33 34 35 36 37 32 04 56 31 2e 30 38 c8 01 51 f9 7f
```

### 5.2 Set time (`0x0002`) — observed

| Field | Type | Meaning |
|---|---|---|
| 1 | fixed32 | Unix time (UTC) |
| 2 | float | timezone offset in hours |

The app sends it once per connection, **on `fda1`** with command type `0x4_`; the device acks it on
`fda2`. Example for 2026-01-01 00:00:00 UTC, UTC+0:

```
7e 44 00 11 00 02 0d 00 b9 55 69 15 00 00 00 00 c5 b4 7f
```

### 5.3 GNSS info (`0x0036` response) — observed

`{3: "UBX 10", 4: fixed32 Unix time, 5: varint, 6: {1: varint, 2: float latitude, 3: float longitude}}`.
This looks like the GNSS receiver model plus the last position/time, probably used for AGPS. Not
needed for file transfer.

## 6. File transfer

### 6.1 Download (device → app) — confirmed

```
fda1  → 7e 01 00 07 00 32 28 dc 7f                        STATUS request
fda1  ← 7e 11 00 07 00 32 2c 86 7f                        STATUS response
fda1  → 7e 21 00 05 …                                     ack
fda1  → 7e 02 00 15 00 29 0a 0c "filelist.txt" 6f ca 7f  FETCH_FILE {1: "filelist.txt"}
fda1  ← 7e 12 00 09 00 29 08 01 8f f5 7f                  accepted {1: 1}
fda1  → 7e 22 00 05 …                                     ack
fda3  ← 7e 80 00 18 00 2b 0a 0c "filelist.txt" 10 88 2d a5 f8 7f
                                                          FILE_HEADER {1: name, 2: 5768 → size 2884}
fda3  → 7e a0 00 05 21 a5 7f                              ack
fda4  ← data packets 0, 1, 2, …
fda3  ← 7e 81 00 07 00 2e d9 b1 7f                        FILE_DONE
fda3  → 7e a1 00 05 …                                     ack
fda2  → 7e 43 00 09 00 2f 08 01 6f a3 7f                  FILE_CONFIRM {1: 1}
fda2  ← 7e 63 00 05 5e 02 7f                              ack
```

- **File size:** field 2 of FILE_HEADER is a zigzag-encoded (`sint`) varint, so the raw value is
  twice the size. `5768` → `2884` bytes.
- **Data packets on `fda4`:** they are not framed or escaped:

  ```
  index (4, BE, starting at 0) | data | checksum (1)
  ```

  `checksum = sum(index + data) & 0xFF`. With MTU 200 each packet carries 192 data bytes; the last
  one is shorter. Concatenating the data of all packets gives exactly the header size. No per-packet
  acks or retransmission were observed; the device streams the whole file.
- The file list is plain text, one `YYYYMMDDhhmmss.fit <size>` entry per line, separated by CRLF.
  FIT files are fetched by these names with the same sequence.
- FILE_CONFIRM does not appear to delete anything: a fetched FIT file is still listed afterwards.
  What happens without FILE_CONFIRM, or with `{1: 0}`, is unknown.

Reference transfers:
- CoospoRide (Android): `Setting.json` (14.6 KB) streamed in about 2 s, roughly 60 kbit/s of file
  data, at a 48.75 ms connection interval with DLE and 1M PHY (§1.1). The connection interval
  probably limits this more than the packet size does.
- coospo-cli (Linux/BlueZ): an 82 KB FIT file took about 8 s, including scanning and connecting.

### 6.2 Upload (app → device) — partially observed, not implemented

The app uploaded a short `Setting.json` fragment that sets the timezone. For
`{"time_profile":{"time_zone":0}}` (32 bytes) the frames would be:

```
fda2  → command 0x002b {1: "Setting.json", 2: 64 (zigzag → 32)}   ← ack on fda2
fda2  → command 0x002c payload: 00 03 + raw JSON bytes (not protobuf)  ← ack on fda2
```

The meaning of the 2-byte prefix `00 03` is unknown, as is how larger files are chunked.

## 7. Service `0xFD00` (`fd0a` → `fd09`) — observed

This service uses a separate, simpler framing:

```
lead (a2 app → device, d2 device → app) | total length (1) | cmd (1) | data | sum8 of all preceding bytes
```

The app sent two queries:

```
fd0a → a2 04 80 26          fd09 ← d2 0b 80 00 <6-byte address> <sum>
fd0a → a2 04 82 28          fd09 ← d2 0a 82 "COOSPO" 31
```

Its purpose is unclear (possibly bootloader or OTA related). File transfer doesn't need it.

## 8. Other notes

- The device's `Setting.json` contains `"display_profile": {"version": "BC600_V1.3", …}`, which
  suggests other current COOSPO models (e.g. the BC600) may share this protocol. Not tested.
- The device stops advertising while another central (e.g. the phone app) is connected to it.
