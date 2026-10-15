"""Freeze as-of public evidence; source connection is read-only and transactionally consistent."""
import json
from dataclasses import asdict

from ..fair_value.engine import Config, books, evaluate, market_at, market_ids_at, table_exists
from ..fair_value.providers import reject_secrets
from ..fair_value.store import read_connection


def freeze(store, source, start, end, step_ms=1000, config=Config()):
    if type(step_ms) is not int or step_ms <= 0 or end < start or (end-start)//step_ms > 100000:
        raise ValueError("invalid replay interval (maximum 100000 timer frames)")
    db = read_connection(source)
    try:
        db.execute("BEGIN")
        required = ("market_versions", "snapshots", "health", "raw_events")
        if any(not table_exists(db, x) for x in required):
            raise ValueError("Lesson 2 versioned metadata/books/health required")
        times = set(range(start, end+1, step_ms)) | {start, end}
        for table, column in (("book_observations", "received_ms"), ("snapshots", "local_receive_timestamp"), ("health", "local_timestamp"), ("market_versions", "observed_ms"), ("fv_raw_odds", "received_ms"), ("raw_events", "local_receive_timestamp")):
            if table_exists(db, table):
                times.update(r[0] for r in db.execute(f"SELECT DISTINCT {column} FROM {table} WHERE {column} BETWEEN ? AND ?", (start, end)))
        if len(times) > 100000:
            raise ValueError("too many frames; split the interval")
        events = []
        for at in sorted(times):
            health = db.execute("SELECT kind FROM health WHERE local_timestamp<=? AND kind IN ('connected','disconnected','stopped') ORDER BY local_timestamp DESC,id DESC LIMIT 1", (at,)).fetchone()
            gap = db.execute("SELECT 1 FROM health WHERE local_timestamp=? AND kind IN ('disconnected','stopped') LIMIT 1", (at,)).fetchone()
            frames = []
            for market_id in market_ids_at(db, at):
                version = market_at(db, market_id, at)
                if not version:
                    continue
                for signal in evaluate(db, market_id, at, config):
                    asset = signal.get("asset_id")
                    if not asset:
                        continue
                    observations = books(db, market_id, asset, end=at)
                    book = observations[-1] if observations else None
                    evidence = []
                    for s in signal.get("sources", []):
                        raw_id = s.get("raw_odds_id")
                        if raw_id and table_exists(db, "fv_raw_odds"):
                            raw = db.execute("SELECT * FROM fv_raw_odds WHERE id=? AND received_ms<=?", (raw_id, at)).fetchone()
                            if raw:
                                evidence.append(dict(raw))
                    frames.append({"time_ms": at, "market_id": market_id, "asset_id": asset,
                                   "market": version["market"], "signal": signal, "book": book["payload"] if book else None,
                                   "book_ref": book["ref"] if book else None, "book_received_ms": book["observed_ms"] if book else None,
                                   "connected": bool(health and health[0] == "connected" and not gap), "raw_odds": evidence})
            trades = []
            for row in db.execute("SELECT * FROM raw_events WHERE local_receive_timestamp=? ORDER BY event_key", (at,)):
                raw = json.loads(row["payload"])
                items = raw if isinstance(raw, list) else [raw]
                for index, trade in enumerate(items):
                    if isinstance(trade, dict) and trade.get("event_type") == "last_trade_price":
                        trades.append({"event_key": row["event_key"], "index": index, "receive_ms": at, "payload": trade})
            event = {"time_ms": at, "frames": frames, "trades": trades}
            reject_secrets(event)
            events.append(event)
        return store.seal(events, {"start_ms": start, "end_ms": end, "step_ms": step_ms, "fair_config": asdict(config),
                                   "source_kind": "public-recordings", "network_calls": 0})
    finally:
        db.close()
