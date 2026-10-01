# coospo-cli

Command-line tool for **COOSPO CS500/CS600** bike computers over Bluetooth LE. It shows device
information, battery level and memory usage, and downloads your activities (`.fit` files) without the
CoospoRide app.

```
$ coospo info
Searching for CS500 or CS600...
Connected to CS500-1234567
Model:     CS500
Serial:    1234567
Hardware:  V1.41
Firmware:  V1.5.0
Battery:   87%
Memory:    25.3M/26.9M used (93%)
```

## Supported devices

| Device | Status |
|---|---|
| COOSPO CS500 | tested (hardware V1.41, firmware V1.5.0) |
| COOSPO CS600 | expected to work (same protocol family), not tested yet |

Older COOSPO models (BC102/107, BC200) use a different protocol (YMODEM over Nordic UART). Use
[xoss_sync](https://github.com/ekspla/xoss_sync) for those.

If you own a CS600 or another model, please report whether it works in an
[issue](https://github.com/blshkv/coospo-cli/issues).

## Installation

Requires Python 3.9+ and a Bluetooth LE adapter. It uses [bleak](https://github.com/hbldh/bleak), so
it should run on Linux (BlueZ), Windows and macOS; so far it's been tested on Linux.

```
pipx install git+https://github.com/blshkv/coospo-cli
```

or, from a checkout:

```
pip install .
```

## Usage

```
coospo info                            show model, serial number, firmware, battery and memory usage
coospo sync                            download new activities into fit_files/
coospo sync -o ~/rides                 ... into another directory
coospo get filelist.txt Setting.json   download specific files
coospo -n CS500-1234567 info           pick one of several devices by name
coospo -a AA:BB:CC:DD:EE:FF info       ... or by Bluetooth address
```

Run `coospo --help` or `coospo COMMAND --help` for all options.

- `sync` first downloads `filelist.txt` (the list of activities on the device), then every `.fit`
  file not already in the output directory. It's safe to interrupt and run again: files are only
  saved after the transfer has been verified, and existing ones are skipped.
- Nothing is ever deleted from the device.
- The exit status is 0 on success and 1 if the device wasn't found or any file failed.

## Troubleshooting

- **Device not found:** the bike computer stops advertising while it is connected to the phone.
  Close CoospoRide or turn off Bluetooth on the phone, and make sure the device is switched on.
- **Several devices nearby:** use `-n` with the full name shown on the device (e.g.
  `CS500-1234567`) or `-a` with its Bluetooth address.

## Protocol

The CS500 doesn't use the Nordic UART/YMODEM protocol of older models. It uses vendor service
`0xFDA0` with HDLC-like frames carrying protobuf payloads, plus a separate raw data channel for file
contents. [PROTOCOL.md](PROTOCOL.md) documents everything known so far.

`tools/gatt_dump.py` lists a device's GATT services and characteristics, which is a quick way to
check whether another model uses the same service:

```
python3 tools/gatt_dump.py CS600
```

### Helping with other models

Support for new devices or features (setting the clock, uploading settings) comes from capturing
what the official app does:

1. On Android, enable **Developer options → Enable Bluetooth HCI snoop log**, then toggle Bluetooth
   off and on.
2. Use the feature in CoospoRide.
3. Run `adb bugreport bugreport.zip`; the log is `FS/data/misc/bluetooth/logs/btsnoop_hci.log`.

**Don't attach the log or the bug report to a public issue.** They contain personal data such as
your location, device addresses and ride history. Describe what you did and share the relevant
frames instead.

## Development

```
pip install -e .
python3 -m unittest discover -s tests
```

## License

GNU General Public License v3.0 or later, see [LICENSE](LICENSE).

## Acknowledgements

Inspired by [xoss_sync](https://github.com/ekspla/xoss_sync), which supports XOSS,
Cycplus and older COOSPO devices.
