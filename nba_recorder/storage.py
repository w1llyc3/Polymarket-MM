import hashlib
import json
import sqlite3
import uuid
import time
from pathlib import Path


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS markets(market_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, updated_ms INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS raw_events(event_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, source TEXT NOT NULL,
          exchange_timestamp INTEGER, local_receive_timestamp INTEGER NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, event_key TEXT NOT NULL REFERENCES raw_events(event_key),
          market_id TEXT NOT NULL, asset_id TEXT NOT NULL, exchange_timestamp INTEGER, local_receive_timestamp INTEGER NOT NULL,
          event_type TEXT NOT NULL, bid TEXT, ask TEXT, depth TEXT NOT NULL, flags TEXT NOT NULL, payload TEXT NOT NULL,
          UNIQUE(event_key,asset_id));
        CREATE INDEX IF NOT EXISTS snapshots_asset ON snapshots(asset_id,id);
        CREATE TABLE IF NOT EXISTS latest_books(asset_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS market_versions(id INTEGER PRIMARY KEY, market_id TEXT NOT NULL,
          observed_ms INTEGER NOT NULL, metadata TEXT NOT NULL, UNIQUE(market_id,observed_ms,metadata));
        CREATE INDEX IF NOT EXISTS market_versions_time ON market_versions(market_id,observed_ms,id);
        CREATE TABLE IF NOT EXISTS book_observations(id INTEGER PRIMARY KEY, observation_key TEXT UNIQUE NOT NULL,
          event_key TEXT NOT NULL, snapshot_id INTEGER, market_id TEXT NOT NULL, asset_id TEXT NOT NULL,
          received_ms INTEGER NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS book_observations_time ON book_observations(asset_id,received_ms,id);
        CREATE TABLE IF NOT EXISTS event_processing(id INTEGER PRIMARY KEY,event_key TEXT NOT NULL,received_ms INTEGER NOT NULL,processed_ms INTEGER NOT NULL,lag_ms INTEGER NOT NULL,UNIQUE(event_key,received_ms));
        CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY, session_id TEXT, local_timestamp INTEGER, kind TEXT, detail TEXT);
        """)
        self.session = str(uuid.uuid4())

    def markets(self, markets, now):
        with self.db:
            self.db.executemany("INSERT INTO markets VALUES(?,?,?) ON CONFLICT(market_id) DO UPDATE SET metadata=excluded.metadata,updated_ms=excluded.updated_ms", [(m["market_id"], encode(m), now) for m in markets])
            self.db.executemany("INSERT OR IGNORE INTO market_versions(market_id,observed_ms,metadata) VALUES(?,?,?)", [(m["market_id"], now, encode(m)) for m in markets])

    def health(self, now, kind, detail=""):
        with self.db:
            self.db.execute("INSERT INTO health(session_id,local_timestamp,kind,detail) VALUES(?,?,?,?)", (self.session, now, kind, detail))

    def key(self, event, source):
        # Timestamp/hash-bearing identical events have a restart-stable identity.
        # Without exchange identity, don't collapse legitimate identical observations.
        identity = encode(event)
        identifiable = (event.get("timestamp") is not None or event.get("hash") or event.get("event_id")
                        or event.get("transaction_hash") or (event.get("id") and event.get("event_type") in {"new_market", "market_resolved"}))
        if not identifiable:
            identity += str(uuid.uuid4())
        return hashlib.sha256((source + identity).encode()).hexdigest()

    def commit(self, event, source, received, snapshots, key=None):
        key = key or self.key(event, source)
        with self.db:
            try:
                exchange = int(event["timestamp"]) if event.get("timestamp") is not None else None
            except (ValueError, TypeError):
                exchange = None
            cur = self.db.execute("INSERT OR IGNORE INTO raw_events VALUES(?,?,?,?,?,?,?)", (key, self.session, source, exchange, received, event.get("event_type", "unknown"), encode(event)))
            for s in snapshots:
                if cur.rowcount:
                    self.db.execute("INSERT INTO snapshots(event_key,market_id,asset_id,exchange_timestamp,local_receive_timestamp,event_type,bid,ask,depth,flags,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (key, s["market_id"], s["asset_id"], s["exchange_timestamp"], s["local_receive_timestamp"], event["event_type"], s["bid"], s["ask"], encode({"bids": s["bids"], "asks": s["asks"]}), encode(s["flags"]), encode(s)))
                self.db.execute("INSERT INTO latest_books VALUES(?,?,?) ON CONFLICT(asset_id) DO UPDATE SET event_key=excluded.event_key,payload=excluded.payload", (s["asset_id"], key, encode(s)))
                snapshot = self.db.execute("SELECT id FROM snapshots WHERE event_key=? AND asset_id=?", (key, s["asset_id"])).fetchone()
                observation_key = hashlib.sha256(encode([key, s["asset_id"], received, s]).encode()).hexdigest()
                self.db.execute("INSERT OR IGNORE INTO book_observations(observation_key,event_key,snapshot_id,market_id,asset_id,received_ms,payload) VALUES(?,?,?,?,?,?,?)", (observation_key, key, snapshot[0] if snapshot else None, s["market_id"], s["asset_id"], received, encode(s)))
            processed = time.time_ns()//1000000
            self.db.execute("INSERT OR IGNORE INTO event_processing(event_key,received_ms,processed_ms,lag_ms) VALUES(?,?,?,?)", (key, received, processed, processed-received))
        return bool(cur.rowcount)

    def contains(self, key):
        return self.db.execute("SELECT 1 FROM raw_events WHERE event_key=?", (key,)).fetchone() is not None

    def close(self):
        self.db.close()
