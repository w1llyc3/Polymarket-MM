"""Offline, read-only evidence report; no network or wallet operations."""
import argparse
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path


def report(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    sessions = []
    for row in c.execute("SELECT session_id,min(local_timestamp) first,max(local_timestamp) last FROM health GROUP BY session_id"):
        start = c.execute("SELECT min(local_timestamp) FROM health WHERE session_id=? AND kind='started'", (row["session_id"],)).fetchone()[0]
        stop = c.execute("SELECT max(local_timestamp) FROM health WHERE session_id=? AND kind='stopped'", (row["session_id"],)).fetchone()[0]
        connected = c.execute("SELECT count(*) FROM health WHERE session_id=? AND kind='connected'", (row["session_id"],)).fetchone()[0]
        verified = c.execute("SELECT min(local_timestamp),max(local_timestamp),count(*) FROM health WHERE session_id=? AND kind='snapshot_verified'", (row["session_id"],)).fetchone()
        local = lambda t: datetime.fromtimestamp(t / 1000, timezone(timedelta(hours=8))).isoformat() if t else None
        sessions.append({"session_id": row["session_id"], "started_china_time": local(start), "stopped_china_time": local(stop),
                         "duration_seconds": ((stop or row["last"]) - start) / 1000 if start else None,
                         "connections": connected, "verified_snapshots": verified[2],
                         "verified_span_seconds": (verified[1]-verified[0])/1000 if verified[0] else 0})
    bad, crossed, locked, empty = 0, 0, 0, 0
    for row in c.execute("SELECT bid,ask,flags FROM snapshots"):
        flags = json.loads(row["flags"])
        if row["bid"] is None or row["ask"] is None:
            empty += 1
            bad += "empty" not in flags
        elif Decimal(row["bid"]) > Decimal(row["ask"]):
            crossed += 1
            bad += "crossed" not in flags
        elif Decimal(row["bid"]) == Decimal(row["ask"]):
            locked += 1
            bad += "locked" not in flags
    events = {r[0]: r[1] for r in c.execute("SELECT event_type,count(*) FROM raw_events GROUP BY event_type")}
    result = {"venue": "polymarket.com", "sqlite_integrity": c.execute("PRAGMA integrity_check").fetchone()[0],
              "sessions": sessions, "event_counts": events,
              "normalized_snapshots": c.execute("SELECT count(*) FROM snapshots").fetchone()[0],
              "assets": c.execute("SELECT count(DISTINCT asset_id) FROM snapshots").fetchone()[0],
              "duplicate_primary_keys": c.execute("SELECT count(*)-count(DISTINCT event_key) FROM raw_events").fetchone()[0],
              "duplicate_identifiable_payloads": c.execute("SELECT count(*) FROM (SELECT source,payload FROM raw_events WHERE exchange_timestamp IS NOT NULL GROUP BY source,payload HAVING count(*)>1)").fetchone()[0],
              "duplicates_skipped": c.execute("SELECT count(*) FROM health WHERE kind='duplicate'").fetchone()[0],
              "bbo": {"crossed": crossed, "locked": locked, "empty": empty, "unmarked_anomalies": bad},
              "disconnects": [dict(r) for r in c.execute("SELECT session_id,local_timestamp,detail FROM health WHERE kind='disconnected'")]}
    c.close()
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/nba.sqlite")
    args = p.parse_args()
    print(json.dumps(report(args.db), indent=2, ensure_ascii=True))
