"""Persistent local note index: note text, content digests, and cached embeddings."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np

PATH = Path(__file__).resolve().parents[1] / '.anki_gemini_index' / 'index.sqlite3'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS notes (
    note_id INTEGER PRIMARY KEY, mod INTEGER, model TEXT NOT NULL,
    fields TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


def digest(*parts):
    return hashlib.sha256('\0'.join(map(str, parts)).encode()).hexdigest()


def unit(vector):
    v = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    if not np.isfinite(norm) or norm == 0:
        raise ValueError('Embedding has zero or invalid length.')
    return v / norm


class Index:
    def __init__(self, path=None):
        self.path = Path(path or PATH)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.db = self._open()
        except sqlite3.DatabaseError as exc:
            # The index is a rebuildable cache; keep the bad file for inspection.
            aside = self.path.with_name(
                f'{self.path.name}.corrupt-{datetime.now():%Y%m%dT%H%M%S}')
            self.path.replace(aside)
            print(f'Retrieval index was unreadable ({exc}); moved to {aside.name} and rebuilding.',
                  file=sys.stderr)
            self.db = self._open()

    def _open(self):
        new = not self.path.exists()
        db = sqlite3.connect(self.path, timeout=30)
        if new:
            os.chmod(self.path, 0o600)
        try:
            db.executescript(SCHEMA)
            db.execute('SELECT count(*) FROM notes').fetchone()
        except sqlite3.DatabaseError:
            db.close()
            raise
        return db

    def close(self):
        self.db.close()

    def mods(self):
        return dict(self.db.execute('SELECT note_id, mod FROM notes'))

    def notes(self, ids=None):
        rows = self.db.execute('SELECT note_id, model, fields, digest FROM notes')
        wanted = None if ids is None else set(ids)
        return {nid: dict(note_id=nid, model=model, fields=json.loads(fields), digest=d)
                for nid, model, fields, d in rows if wanted is None or nid in wanted}

    def upsert(self, rows):
        """rows: iterable of (note_id, mod, model, fields, digest)."""
        with self.db:
            self.db.executemany(
                'INSERT INTO notes VALUES (?, ?, ?, ?, ?) ON CONFLICT(note_id) DO UPDATE SET '
                'mod=excluded.mod, model=excluded.model, fields=excluded.fields, '
                'digest=excluded.digest',
                [(nid, mod, model, json.dumps(fields, ensure_ascii=False, sort_keys=True), d)
                 for nid, mod, model, fields, d in rows])

    def remove_missing(self, present):
        present = set(present)
        gone = [(nid,) for nid in self.mods() if nid not in present]
        with self.db:
            self.db.executemany('DELETE FROM notes WHERE note_id = ?', gone)
        return len(gone)

    def vectors(self, keys):
        keys = list(set(keys))
        found = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ','.join('?' * len(chunk))
            for key, blob in self.db.execute(
                    f'SELECT key, vector FROM vectors WHERE key IN ({marks})', chunk):
                found[key] = np.frombuffer(blob, dtype=np.float32)
        return found

    def store_vectors(self, pairs):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO vectors VALUES (?, ?)',
                                [(key, unit(v).tobytes()) for key, v in pairs])

    def prune_vectors(self, keep):
        """Drop cached vectors whose keys are not in keep."""
        with self.db:
            self.db.execute('CREATE TEMP TABLE IF NOT EXISTS keep (key TEXT PRIMARY KEY)')
            self.db.execute('DELETE FROM keep')
            self.db.executemany('INSERT OR IGNORE INTO keep VALUES (?)', [(k,) for k in keep])
            return self.db.execute(
                'DELETE FROM vectors WHERE key NOT IN (SELECT key FROM keep)').rowcount

    def meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return default if row is None else row[0]

    def set_meta(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, str(value)))
