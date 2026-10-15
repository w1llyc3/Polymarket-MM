import hashlib
import json
import sqlite3
from pathlib import Path

from ..storage import Store, encode
from .providers import reject_secrets


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class FairStore:
    def __init__(self, path):
        self.base = Store(path)
        self.db = self.base.db
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS fv_raw_odds(id TEXT PRIMARY KEY, provider TEXT NOT NULL, received_ms INTEGER NOT NULL,
          data_kind TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fv_odds(id INTEGER PRIMARY KEY, raw_id TEXT NOT NULL, provider TEXT NOT NULL,
          source_id TEXT NOT NULL, event_id TEXT NOT NULL, received_ms INTEGER NOT NULL, updated_ms INTEGER,
          data_kind TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(raw_id,source_id,event_id,payload));
        CREATE INDEX IF NOT EXISTS fv_odds_time ON fv_odds(received_ms,id);
        CREATE TABLE IF NOT EXISTS fv_signals(id TEXT PRIMARY KEY, market_id TEXT NOT NULL, asset_id TEXT,
          decision_ms INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fv_candidates(id TEXT PRIMARY KEY, signal_id TEXT NOT NULL,
          market_id TEXT NOT NULL, asset_id TEXT NOT NULL, decision_ms INTEGER NOT NULL, side TEXT NOT NULL,
          quote_price TEXT NOT NULL, book_ref TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fv_markouts(candidate_id TEXT NOT NULL, horizon_s INTEGER NOT NULL,
          status TEXT NOT NULL, evaluated_ms INTEGER NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(candidate_id,horizon_s));
        """)

    def close(self):
        self.base.close()

    def ingest(self, batch):
        reject_secrets(batch.raw)
        reject_secrets(batch.quotes)
        raw_id = digest([batch.provider, batch.received_ms, batch.raw, batch.quotes, batch.data_kind])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO fv_raw_odds VALUES(?,?,?,?,?)", (raw_id, batch.provider, batch.received_ms, batch.data_kind, encode(batch.raw)))
            for q in batch.quotes:
                try:
                    updated = int(q["updated_ms"])
                    if not 0 <= updated <= 2 ** 63 - 1:
                        updated = None
                except (KeyError, ValueError, TypeError):
                    updated = None
                self.db.execute("INSERT OR IGNORE INTO fv_odds(raw_id,provider,source_id,event_id,received_ms,updated_ms,data_kind,payload) VALUES(?,?,?,?,?,?,?,?)", (raw_id, batch.provider, str(q.get("source_id", "")), str(q.get("event_id", "")), batch.received_ms, updated, batch.data_kind, encode(q)))
        return raw_id

    def persist(self, signal):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO fv_signals VALUES(?,?,?,?,?,?)", (signal["id"], signal["market_id"], signal.get("asset_id"), signal["decision_ms"], signal["status"], encode(signal)))
            for candidate in signal["candidates"]:
                self.db.execute("INSERT OR IGNORE INTO fv_candidates VALUES(?,?,?,?,?,?,?,?,?)", (candidate["id"], signal["id"], candidate["market_id"], candidate["asset_id"], candidate["decision_ms"], candidate["side"], candidate["quote_price"], candidate["book_ref"], encode(candidate)))


def read_connection(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db
