"""SQLite index of the activities already handled, so none is downloaded or uploaded twice."""

import os
import sqlite3
import time

MAX_BACKOFF = 6 * 3600   # longest wait between post-sync attempts for one file

SCHEMA = '''
CREATE TABLE IF NOT EXISTS devices (
    serial TEXT PRIMARY KEY,
    baselined_at REAL
);
CREATE TABLE IF NOT EXISTS files (
    serial TEXT NOT NULL,
    name TEXT NOT NULL,
    skipped INTEGER NOT NULL DEFAULT 0,       -- already on the device at baseline, never fetched
    downloaded_at REAL,
    path TEXT,
    staged INTEGER NOT NULL DEFAULT 0,        -- path is in the staging directory
    hook_done_at REAL,
    hook_attempts INTEGER NOT NULL DEFAULT 0,
    hook_next REAL NOT NULL DEFAULT 0,        -- earliest time for the next attempt
    PRIMARY KEY (serial, name)
);
'''


class Index:
    def __init__(self, path):
        if path != ':memory:':
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def is_baselined(self, serial):
        row = self.db.execute('SELECT baselined_at FROM devices WHERE serial = ?', (serial,)).fetchone()
        return bool(row and row[0])

    def any_baselined(self):
        return self.db.execute('SELECT 1 FROM devices WHERE baselined_at IS NOT NULL').fetchone() is not None

    def set_baselined(self, serial):
        with self.db:
            self.db.execute('INSERT INTO devices (serial, baselined_at) VALUES (?, ?) '
                            'ON CONFLICT(serial) DO UPDATE SET baselined_at = excluded.baselined_at',
                            (serial, time.time()))

    def known(self, serial):
        '''Names of all files of the device that were skipped or downloaded.'''
        rows = self.db.execute('SELECT name FROM files WHERE serial = ?', (serial,))
        return {name for (name,) in rows}

    def skip(self, serial, names):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO files (serial, name, skipped) VALUES (?, ?, 1)',
                                [(serial, n) for n in names])

    def add_downloaded(self, serial, name, path, staged):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO files (serial, name) VALUES (?, ?)', (serial, name))
            self.db.execute('UPDATE files SET downloaded_at = ?, path = ?, staged = ? '
                            'WHERE serial = ? AND name = ?', (time.time(), path, int(staged), serial, name))

    def hooks_due(self, now=None):
        '''Downloaded files whose post-sync command has not succeeded yet: (serial, name, path, staged).'''
        rows = self.db.execute(
            'SELECT serial, name, path, staged FROM files '
            'WHERE downloaded_at IS NOT NULL AND hook_done_at IS NULL AND hook_next <= ? '
            'ORDER BY name', (time.time() if now is None else now,))
        return rows.fetchall()

    def hook_done(self, serial, name):
        with self.db:
            self.db.execute('UPDATE files SET hook_done_at = ? WHERE serial = ? AND name = ?',
                            (time.time(), serial, name))

    def hook_failed(self, serial, name, now=None):
        now = time.time() if now is None else now
        with self.db:
            attempts, = self.db.execute('SELECT hook_attempts FROM files WHERE serial = ? AND name = ?',
                                        (serial, name)).fetchone()
            self.db.execute('UPDATE files SET hook_attempts = ?, hook_next = ? WHERE serial = ? AND name = ?',
                            (attempts + 1, now + min(60 * 2 ** attempts, MAX_BACKOFF), serial, name))
