"""Command-line tool for COOSPO CS500/CS600 bike computers over Bluetooth LE."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("coospo-cli")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+unknown"
