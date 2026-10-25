"""Content-addressed immutable offline inputs; lane order and timing stay local.

Only new offline experiments opt in. Public collectors and old databases retain
their original schema. Payloads are byte-identical UTF-8, losslessly compressed.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import zlib


class ResourceConnection(sqlite3.Connection):
    def close(self):
        try:
            super().close()
        finally:
            pool = getattr(self, '_input_pool', None)
            if pool is not None:
                pool.close()
                self._input_pool = None


def _pool_path(database, relative):
    parent = Path(database).resolve().parent
    p = Path(relative)
    if p.is_absolute() or p.name != 'shared-inputs.sqlite' or len(p.parts) > 3:
        raise ValueError('Invalid shared input dependency')
    resolved = (parent / p).resolve()
    if resolved.parent not in (parent, parent.parent, parent.parent.parent):
        raise ValueError('Shared input dependency outside experiment')
    if (parent / p).is_symlink():
        raise ValueError('Redirected shared input dependency')
    return resolved


def attach(db, database, shared=None, writable=False):
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE name='ip_input_storage'").fetchone()
    if existing:
        relative = db.execute('SELECT pool_path FROM ip_input_storage WHERE id=1').fetchone()[0]
        path = _pool_path(database, relative)
        if shared is not None and path != Path(shared).resolve():
            raise ValueError('Shared input dependency cannot change')
    elif shared is not None:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ip_inputs'").fetchone():
            raise ValueError('No implicit migration of existing input history')
        relative = Path(os.path.relpath(Path(shared).resolve(), Path(database).resolve().parent)).as_posix()
        path = _pool_path(database, relative)
    else:
        return
    writing = shared is not None or writable
    # Reopening a lane requires its authoritative dependency to exist. Missing
    # history is never recreated as an empty pool.
    if existing and not path.is_file():
        raise FileNotFoundError('Missing immutable shared input pool: ' + str(path))
    if writing:
        path.parent.mkdir(parents=True, exist_ok=True)
        pool = sqlite3.connect(path, timeout=60)
        pool.execute('PRAGMA journal_mode=WAL')
        pool.execute('PRAGMA synchronous=FULL')
        pool.executescript("""
        CREATE TABLE IF NOT EXISTS inputs(digest TEXT PRIMARY KEY,payload BLOB NOT NULL);
        CREATE TRIGGER IF NOT EXISTS inputs_u BEFORE UPDATE ON inputs BEGIN SELECT RAISE(ABORT,'immutable shared input'); END;
        CREATE TRIGGER IF NOT EXISTS inputs_d BEFORE DELETE ON inputs BEGIN SELECT RAISE(ABORT,'immutable shared input'); END;
        """)
    else:
        pool = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=60)
    db._input_pool = pool

    def read(digest):
        row = pool.execute('SELECT payload FROM inputs WHERE digest=?', (digest,)).fetchone()
        if row is None:
            raise ValueError('Missing shared input: ' + digest)
        raw = zlib.decompress(row[0])
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Corrupt shared input: ' + digest)
        return raw.decode('utf-8')

    db.create_function('nba_input_read', 1, read)
    if not writing:
        return

    def put(payload):
        raw = payload.encode('utf-8'); digest = hashlib.sha256(raw).hexdigest()
        # Durable blob first, local reference second. A rolled-back lane may
        # leave an unreferenced blob, never a reference to an uncommitted blob.
        with pool:
            pool.execute('INSERT OR IGNORE INTO inputs VALUES(?,?)', (digest, zlib.compress(raw, 1)))
        if read(digest) != payload:
            raise ValueError('Shared content collision')
        return digest

    db.create_function('nba_input_put', 1, put)
    if not existing:
        db.execute('CREATE TABLE ip_input_storage(id INTEGER PRIMARY KEY CHECK(id=1),pool_path TEXT NOT NULL)')
        db.execute('INSERT INTO ip_input_storage VALUES(1,?)', (relative,))
        db.commit()
        db.executescript("""
        CREATE TABLE ip_input_refs(seq INTEGER PRIMARY KEY,id TEXT UNIQUE NOT NULL,time_ms INTEGER NOT NULL,digest TEXT NOT NULL);
        CREATE INDEX ip_input_refs_time ON ip_input_refs(time_ms,seq);
        CREATE VIEW ip_inputs AS SELECT seq AS rowid,id,time_ms,nba_input_read(digest) AS payload FROM ip_input_refs;
        CREATE TRIGGER ip_inputs_i INSTEAD OF INSERT ON ip_inputs BEGIN
          INSERT INTO ip_input_refs(id,time_ms,digest) VALUES(NEW.id,NEW.time_ms,nba_input_put(NEW.payload)); END;
        CREATE TRIGGER ip_inputs_u INSTEAD OF UPDATE ON ip_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
        CREATE TRIGGER ip_inputs_d INSTEAD OF DELETE ON ip_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
        CREATE TRIGGER ip_input_refs_u BEFORE UPDATE ON ip_input_refs BEGIN SELECT RAISE(ABORT,'immutable input reference'); END;
        CREATE TRIGGER ip_input_refs_d BEFORE DELETE ON ip_input_refs BEGIN SELECT RAISE(ABORT,'immutable input reference'); END;
        CREATE TRIGGER ip_input_storage_u BEFORE UPDATE ON ip_input_storage BEGIN SELECT RAISE(ABORT,'immutable dependency'); END;
        CREATE TRIGGER ip_input_storage_d BEFORE DELETE ON ip_input_storage BEGIN SELECT RAISE(ABORT,'immutable dependency'); END;
        """)


def ensure_inputs(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='ip_inputs'").fetchone():
        return
    db.executescript("""
    CREATE TABLE ip_inputs(id TEXT PRIMARY KEY,time_ms INTEGER,payload TEXT);
    CREATE TRIGGER ip_inputs_u BEFORE UPDATE ON ip_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
    CREATE TRIGGER ip_inputs_d BEFORE DELETE ON ip_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
    """)
