"""Causal public-book tape without sportsbook odds dependencies."""
import json
from ..fair_value.engine import books, market_at, market_ids_at, table_exists
from ..fair_value.store import read_connection, digest
from .sports import normalize


def freeze(store, source, start, end, sports_source=None, contracts=None, step_ms=1000):
    if type(step_ms) is not int or step_ms <= 0 or end < start or (end-start)//step_ms > 100000:
        raise ValueError("invalid bounded book replay interval")
    db = read_connection(source)
    sports = read_connection(sports_source) if sports_source else None
    try:
        db.execute("BEGIN")
        if sports:
            sports.execute("BEGIN")
        times = set(range(start, end+1, step_ms)) | {start, end}
        for table, column in (("book_observations", "received_ms"), ("health", "local_timestamp"), ("market_versions", "observed_ms"), ("raw_events", "local_receive_timestamp")):
            times.update(r[0] for r in db.execute(f"SELECT DISTINCT {column} FROM {table} WHERE {column} BETWEEN ? AND ?", (start, end)))
        measured_processing = table_exists(db, "event_processing")
        if measured_processing:
            times.update(r[0] for r in db.execute("SELECT DISTINCT processed_ms FROM event_processing WHERE processed_ms BETWEEN ? AND ?", (start, end)))
        if sports:
            times.update(r[0] for r in sports.execute("SELECT DISTINCT receive_ms FROM sports_raw WHERE receive_ms BETWEEN ? AND ?", (start, end)))
        if len(times) > 100000:
            raise ValueError("too many event buckets")
        events = []
        for at in sorted(times):
            health = db.execute("SELECT kind FROM health WHERE local_timestamp<=? AND kind IN ('connected','disconnected','stopped') ORDER BY local_timestamp DESC,id DESC LIMIT 1", (at,)).fetchone()
            frames = []
            for market_id in market_ids_at(db, at):
                version = market_at(db, market_id, at)
                if not version:
                    continue
                for outcome in version["market"]["outcomes"]:
                    asset = outcome["asset_id"]
                    observations = books(db, market_id, asset, end=at)
                    observation, processing = None, None
                    for candidate in reversed(observations):
                        raw_processing = db.execute("SELECT * FROM event_processing WHERE received_ms=? AND event_key=? ORDER BY id DESC LIMIT 1", (candidate["observed_ms"], candidate["event_key"])).fetchone() if measured_processing else None
                        # Versions predating telemetry remain readable research,
                        # but UNKNOWN backlog cannot authorize fresh quotes.
                        if not measured_processing or not raw_processing or raw_processing["processed_ms"] <= at:
                            observation, processing = candidate, raw_processing
                            break
                    contract = (contracts or {}).get(market_id, {})
                    sporting = {}
                    if sports and contract.get("game_id"):
                        # Only this game's already-received updates are eligible.
                        for row in sports.execute("SELECT * FROM sports_raw WHERE receive_ms<=? ORDER BY receive_ms DESC,id DESC", (at,)):
                            try:
                                raw = json.loads(row["payload"])
                                if str(raw.get("gameId")) == str(contract["game_id"]):
                                    sporting = normalize(raw, row["receive_ms"], contract)
                                    break
                            except (ValueError, TypeError, AttributeError):
                                continue
                    frames.append({"strategy_mode": "BOOK_DRIVEN", "time_ms": at, "received_ms": processing["received_ms"] if processing else at, "processed_ms": processing["processed_ms"] if processing else None, "backlog_ms": processing["lag_ms"] if processing else None,
                                   "processing_assumption": "recorder persistence lag measured before commit" if processing else "UNKNOWN original processing backlog; cannot authorize quotes",
                                   "asset_id": asset, "market_id": market_id, "market": version["market"], "market_observed_ms": version["observed_ms"],
                                   "book": observation["payload"] if observation else None, "book_ref": observation["ref"] if observation else None,
                                   "book_received_ms": observation["observed_ms"] if observation else None,
                                   "connected": bool(health and health[0] == "connected"), "sports": sporting})
            trades = []
            for row in db.execute("SELECT * FROM raw_events WHERE local_receive_timestamp=? AND event_type='last_trade_price' ORDER BY event_key", (at,)):
                p = json.loads(row["payload"])
                try:
                    trades.append({"event_id": row["event_key"], "asset_id": p["asset_id"], "condition_id": p["market"], "side": p["side"], "price": p["price"], "size": p["size"], "exchange_ms": int(p["timestamp"])})
                except (KeyError, TypeError, ValueError):
                    continue
            events.append({"id": digest(["book-input-v2", str(source), at, frames, trades]), "time_ms": at, "frames": frames, "trades": trades, "provenance": "public-recordings; no odds"})
        return store.seal(events, {"mode": "BOOK_DRIVEN", "source_kind": "public-recordings", "start_ms": start, "end_ms": end, "step_ms": step_ms, "sports_contracts": contracts or {}, "network_calls": 0})
    finally:
        db.close()
        if sports:
            sports.close()
