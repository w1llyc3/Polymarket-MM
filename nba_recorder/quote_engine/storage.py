import json
import sqlite3
from pathlib import Path

from ..storage import encode
from ..fair_value.store import digest


class PaperStore:
    @classmethod
    def readonly(cls, path):
        from ..fair_value.store import read_connection
        store = cls.__new__(cls)
        store.db = read_connection(path)
        return store

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS qe_tapes(id TEXT PRIMARY KEY, manifest TEXT NOT NULL, sealed INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS qe_input(tape_id TEXT NOT NULL, seq INTEGER NOT NULL, time_ms INTEGER NOT NULL,
          event_hash TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(tape_id,seq));
        CREATE TRIGGER IF NOT EXISTS qe_no_edit_input BEFORE UPDATE ON qe_input BEGIN SELECT RAISE(ABORT,'immutable tape'); END;
        CREATE TRIGGER IF NOT EXISTS qe_no_delete_input BEFORE DELETE ON qe_input BEGIN SELECT RAISE(ABORT,'immutable tape'); END;
        CREATE TRIGGER IF NOT EXISTS qe_no_append_sealed BEFORE INSERT ON qe_input WHEN (SELECT sealed FROM qe_tapes WHERE id=NEW.tape_id)=1 BEGIN SELECT RAISE(ABORT,'sealed tape'); END;
        CREATE TRIGGER IF NOT EXISTS qe_no_edit_sealed BEFORE UPDATE ON qe_tapes WHEN OLD.sealed=1 BEGIN SELECT RAISE(ABORT,'sealed tape'); END;
        CREATE TRIGGER IF NOT EXISTS qe_no_delete_tape BEFORE DELETE ON qe_tapes BEGIN SELECT RAISE(ABORT,'immutable tape'); END;
        CREATE TABLE IF NOT EXISTS qe_runs(id TEXT PRIMARY KEY, tape_id TEXT NOT NULL, config TEXT NOT NULL,
          cursor INTEGER NOT NULL, state TEXT NOT NULL, real_submission_calls INTEGER NOT NULL CHECK(real_submission_calls=0));
        CREATE TABLE IF NOT EXISTS qe_accounts(run_id TEXT NOT NULL,lane TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(run_id,lane));
        CREATE TABLE IF NOT EXISTS qe_orders(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,lane TEXT NOT NULL,asset_id TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS qe_journal(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,lane TEXT NOT NULL,
          input_seq INTEGER NOT NULL,time_ms INTEGER NOT NULL,kind TEXT NOT NULL,order_id TEXT,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS qe_decisions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,lane TEXT NOT NULL,
          input_seq INTEGER NOT NULL,time_ms INTEGER NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS qe_marks(anchor_id TEXT NOT NULL,kind TEXT NOT NULL,horizon_s INTEGER NOT NULL,
          run_id TEXT NOT NULL,lane TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(anchor_id,kind,horizon_s));
        CREATE TRIGGER IF NOT EXISTS qe_no_edit_journal BEFORE UPDATE ON qe_journal BEGIN SELECT RAISE(ABORT,'append-only journal'); END;
        CREATE TRIGGER IF NOT EXISTS qe_no_delete_journal BEFORE DELETE ON qe_journal BEGIN SELECT RAISE(ABORT,'append-only journal'); END;
        CREATE TRIGGER IF NOT EXISTS qe_no_edit_decision BEFORE UPDATE ON qe_decisions BEGIN SELECT RAISE(ABORT,'immutable decision'); END;
        CREATE TRIGGER IF NOT EXISTS qe_no_delete_decision BEFORE DELETE ON qe_decisions BEGIN SELECT RAISE(ABORT,'immutable decision'); END;
        CREATE INDEX IF NOT EXISTS qe_journal_run ON qe_journal(run_id,id);
        CREATE TABLE IF NOT EXISTS qe_consumed_trades(run_id TEXT,lane TEXT,event_key TEXT,item_index INTEGER,PRIMARY KEY(run_id,lane,event_key,item_index));
        """)

    def close(self):
        self.db.close()

    def seal(self, events, manifest):
        from ..fair_value.providers import reject_secrets
        reject_secrets(events)
        reject_secrets(manifest)
        previous, rows = "", []
        last_time = -1
        for seq, event in enumerate(events, 1):
            if type(event.get("time_ms")) is not int or event["time_ms"] <= last_time:
                raise ValueError("tape requires strictly increasing receive-time buckets")
            last_time = event["time_ms"]
            previous = digest([previous, seq, event])
            rows.append((seq, event["time_ms"], previous, encode(event)))
        manifest = {**manifest, "count": len(rows), "chain_hash": previous, "format_version": "lesson4-v1"}
        tape_id = digest(manifest)
        if self.db.execute("SELECT 1 FROM qe_tapes WHERE id=?", (tape_id,)).fetchone():
            self.verify(tape_id)
            return tape_id
        with self.db:
            self.db.execute("INSERT INTO qe_tapes VALUES(?,?,0)", (tape_id, encode(manifest)))
            self.db.executemany("INSERT INTO qe_input VALUES(?,?,?,?,?)", [(tape_id, *row) for row in rows])
            self.db.execute("UPDATE qe_tapes SET sealed=1 WHERE id=?", (tape_id,))
        return tape_id

    def verify(self, tape_id):
        row = self.db.execute("SELECT * FROM qe_tapes WHERE id=?", (tape_id,)).fetchone()
        if not row or not row["sealed"]:
            raise ValueError("sealed tape required")
        manifest, previous, count, last_time = json.loads(row["manifest"]), "", 0, -1
        for event in self.db.execute("SELECT * FROM qe_input WHERE tape_id=? ORDER BY seq", (tape_id,)):
            count += 1
            payload = json.loads(event["payload"])
            previous = digest([previous, count, payload])
            if event["seq"] != count or event["event_hash"] != previous or event["time_ms"] != payload["time_ms"] or event["time_ms"] < last_time:
                raise ValueError("tape integrity/time-order failure")
            last_time = event["time_ms"]
        if count != manifest["count"] or previous != manifest["chain_hash"] or digest(manifest) != tape_id:
            raise ValueError("tape manifest mismatch")
        return manifest
