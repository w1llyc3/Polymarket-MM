import sqlite3
from pathlib import Path


class ExecutionStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS pe_control(id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT CHECK(mode='PAPER'),state TEXT,clock_ms INTEGER,config TEXT,account TEXT);
        CREATE TABLE IF NOT EXISTS pe_orders(id TEXT PRIMARY KEY,state TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS pe_journal(seq INTEGER PRIMARY KEY,time_ms INTEGER,kind TEXT,order_id TEXT,payload TEXT);
        CREATE TRIGGER IF NOT EXISTS pe_journal_no_update BEFORE UPDATE ON pe_journal BEGIN SELECT RAISE(ABORT,'append-only journal'); END;
        CREATE TRIGGER IF NOT EXISTS pe_journal_no_delete BEFORE DELETE ON pe_journal BEGIN SELECT RAISE(ABORT,'append-only journal'); END;
        CREATE TABLE IF NOT EXISTS pe_exchange(id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS pe_fills(event_id TEXT PRIMARY KEY,order_id TEXT,time_ms INTEGER,size TEXT);
        CREATE TABLE IF NOT EXISTS pe_pending(id INTEGER PRIMARY KEY,due_ms INTEGER,kind TEXT,order_id TEXT,payload TEXT,done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pe_seen_reports(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS pe_latency(id INTEGER PRIMARY KEY,time_ms INTEGER,kind TEXT,duration_ms INTEGER,order_id TEXT,provenance TEXT CHECK(provenance='synthetic_paper_clock'));
        CREATE TABLE IF NOT EXISTS pe_risks(id INTEGER PRIMARY KEY,time_ms INTEGER,order_id TEXT,reason TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS pe_stream(tape_id TEXT PRIMARY KEY,cursor INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS pe_quotes(id TEXT PRIMARY KEY,time_ms INTEGER,payload TEXT);
        CREATE TABLE IF NOT EXISTS pe_frame_done(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS pe_trade_processed(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS pe_reference(asset_id TEXT PRIMARY KEY,time_ms INTEGER,mid TEXT);
        CREATE TABLE IF NOT EXISTS pe_risk_frames(asset_id TEXT PRIMARY KEY,time_ms INTEGER,payload TEXT);
        CREATE TRIGGER IF NOT EXISTS pe_quotes_no_update BEFORE UPDATE ON pe_quotes BEGIN SELECT RAISE(ABORT,'immutable quote decision'); END;
        CREATE TRIGGER IF NOT EXISTS pe_quotes_no_delete BEFORE DELETE ON pe_quotes BEGIN SELECT RAISE(ABORT,'immutable quote decision'); END;
        """)

    def close(self):
        self.db.close()
