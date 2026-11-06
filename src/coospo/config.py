"""Locations of the config file, state and data, and the config file loader (XDG base directories)."""

import os
import sys
import tomllib


def _xdg(variable, fallback):
    return os.path.join(os.environ.get(variable) or os.path.expanduser(fallback), 'coospo')


def config_path():
    return os.path.join(_xdg('XDG_CONFIG_HOME', '~/.config'), 'config.toml')


def state_dir():
    return _xdg('XDG_STATE_HOME', '~/.local/state')


def data_dir():
    return _xdg('XDG_DATA_HOME', '~/.local/share')


def index_path():
    return os.path.join(state_dir(), 'index.db')


def staging_dir():
    return os.path.join(state_dir(), 'staging')


def load_config(path, known_keys):
    '''Read a TOML file; keys use the option names with '_' instead of '-'. A missing file is empty.'''
    try:
        with open(path, 'rb') as file:
            config = tomllib.load(file)
    except FileNotFoundError:
        return {}
    for key in config.keys() - known_keys:
        print(f"Warning: {path}: unknown setting '{key}'", file=sys.stderr)
    return {k: v for k, v in config.items() if k in known_keys}
