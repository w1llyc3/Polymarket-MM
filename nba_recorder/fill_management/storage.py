def initialize(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS l6_events(seq INTEGER PRIMARY KEY,id TEXT UNIQUE,run_id TEXT,lane TEXT,time_ms INTEGER,kind TEXT,payload TEXT);
    CREATE INDEX IF NOT EXISTS l6_events_account ON l6_events(run_id,lane,seq);
    CREATE TRIGGER IF NOT EXISTS l6_no_edit_event BEFORE UPDATE ON l6_events BEGIN SELECT RAISE(ABORT,'immutable accounting event'); END;
    CREATE TRIGGER IF NOT EXISTS l6_no_delete_event BEFORE DELETE ON l6_events BEGIN SELECT RAISE(ABORT,'immutable accounting event'); END;
    CREATE TABLE IF NOT EXISTS l6_accounts(run_id TEXT,lane TEXT,payload TEXT,PRIMARY KEY(run_id,lane));
    CREATE TABLE IF NOT EXISTS l6_closures(id TEXT PRIMARY KEY,run_id TEXT,lane TEXT,time_ms INTEGER,payload TEXT);
    CREATE TABLE IF NOT EXISTS l6_valuations(run_id TEXT,lane TEXT,input_seq INTEGER,time_ms INTEGER,payload TEXT,PRIMARY KEY(run_id,lane,input_seq));
    CREATE TABLE IF NOT EXISTS l6_candidates(id TEXT PRIMARY KEY,run_id TEXT,lane TEXT,time_ms INTEGER,payload TEXT);
    CREATE TRIGGER IF NOT EXISTS l6_no_edit_candidate BEFORE UPDATE ON l6_candidates BEGIN SELECT RAISE(ABORT,'immutable candidate'); END;
    CREATE TRIGGER IF NOT EXISTS l6_no_delete_candidate BEFORE DELETE ON l6_candidates BEGIN SELECT RAISE(ABORT,'immutable candidate'); END;
    CREATE TABLE IF NOT EXISTS l6_daily(run_id TEXT,lane TEXT,day TEXT,payload TEXT,PRIMARY KEY(run_id,lane,day));
    CREATE TABLE IF NOT EXISTS l6_order_status(id TEXT PRIMARY KEY,run_id TEXT,lane TEXT,state TEXT,
        submitted_quantity TEXT,filled_quantity TEXT,unfilled_quantity TEXT,working_quantity TEXT,cancelled_quantity TEXT,expired_quantity TEXT,payload TEXT);
    """)
