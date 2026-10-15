"""v2.1: one strategy-availability clock for every input branch.

Exchange time is provenance / causal paper matching time. Processing completion
is strategy availability. Unknown processing never exposes an input to strategy.
Cross-channel same-ms order is unproven: snapshot gates are applied before
quotes; public prints cannot fill same-ms new orders.
"""
import json
from ..fair_value.engine import books, table_exists
from ..fair_value.store import read_connection, digest
from .sports import normalize


def available(received, processed):
    if type(received) is int and type(processed) is int and processed >= received:
        return processed
    return None


def freeze(store, source, start, end, sports_source=None, contracts=None, step_ms=1000):
    if type(step_ms) is not int or step_ms <= 0 or end < start or (end-start)//step_ms > 100000:
        raise ValueError("invalid bounded book replay interval")
    db = read_connection(source)
    sports = read_connection(sports_source) if sports_source else None
    try:
        db.execute("BEGIN")
        if sports:
            sports.execute("BEGIN")
        metadata = [dict(r) for r in db.execute("SELECT * FROM market_versions WHERE observed_ms<=?", (end,))]
        health = [dict(r) for r in db.execute("SELECT * FROM health WHERE local_timestamp<=? AND kind IN ('connected','disconnected','stopped')", (end,))]
        processing = {(r["event_key"], r["received_ms"]): dict(r) for r in db.execute("SELECT * FROM event_processing")} if table_exists(db, "event_processing") else {}
        raw_trades = [dict(r) for r in db.execute("SELECT * FROM raw_events WHERE event_type='last_trade_price' AND local_receive_timestamp<=?", (end,))]
        sports_rows = [dict(r) for r in sports.execute("SELECT * FROM sports_raw WHERE receive_ms<=?", (end,))] if sports else []
        unknown = {"metadata": 0, "health": 0, "books": 0, "trades": 0, "sports": 0}
        times = set(range(start, end+1, step_ms)) | {start, end}
        def stamp(row, received_key, kind):
            row["strategy_available_ms"] = available(row.get(received_key), row.get("processed_ms"))
            if row["strategy_available_ms"] is None:
                unknown[kind] += 1
            elif start <= row["strategy_available_ms"] <= end:
                times.add(row["strategy_available_ms"])
        for row in metadata:
            stamp(row, "observed_ms", "metadata")
        for row in health:
            stamp(row, "local_timestamp", "health")
        for row in sports_rows:
            stamp(row, "receive_ms", "sports")
        trade_buckets = {}
        for row in raw_trades:
            proof = processing.get((row["event_key"], row["local_receive_timestamp"]), {})
            row["processed_ms"] = proof.get("processed_ms")
            stamp(row, "local_receive_timestamp", "trades")
            if row["strategy_available_ms"] is not None and start <= row["strategy_available_ms"] <= end:
                trade_buckets.setdefault(row["strategy_available_ms"], []).append(row)
        observations = {}
        for version in metadata:
            market = json.loads(version["metadata"])
            for outcome in market["outcomes"]:
                key = (version["market_id"], outcome["asset_id"])
                if key in observations:
                    continue
                observations[key] = books(db, *key, end=end)
                for candidate in observations[key]:
                    proof = processing.get((candidate["event_key"], candidate["observed_ms"]), {})
                    candidate["processed_ms"] = proof.get("processed_ms")
                    stamp(candidate, "observed_ms", "books")
        if len(times) > 100000:
            raise ValueError("too many availability buckets")
        events = []
        for at in sorted(times):
            eligible_health = [r for r in health if r["strategy_available_ms"] is not None and r["strategy_available_ms"] <= at]
            latest_health = max(eligible_health, key=lambda r: (r["strategy_available_ms"], r["id"])) if eligible_health else None
            # A same-ms disconnect/stopped from another callback wins a connect.
            gap = latest_health and any(r["strategy_available_ms"] == latest_health["strategy_available_ms"] and r["kind"] != "connected" for r in eligible_health)
            frames = []
            for market_id in sorted({r["market_id"] for r in metadata}):
                versions = [r for r in metadata if r["market_id"] == market_id and r["strategy_available_ms"] is not None and r["strategy_available_ms"] <= at]
                if not versions:
                    continue
                version = max(versions, key=lambda r: (r["strategy_available_ms"], r["id"]))
                market = json.loads(version["metadata"])
                for outcome in market["outcomes"]:
                    asset = outcome["asset_id"]
                    eligible = [o for o in observations[(market_id, asset)] if o["strategy_available_ms"] is not None and o["strategy_available_ms"] <= at]
                    observation = max(eligible, key=lambda r: (r["strategy_available_ms"], r["observed_ms"])) if eligible else None
                    contract = (contracts or {}).get(market_id, {})
                    sporting = {}
                    for row in sorted(sports_rows, key=lambda r: (r["strategy_available_ms"] if r["strategy_available_ms"] is not None else -1, r["id"]), reverse=True):
                        if row["strategy_available_ms"] is None or row["strategy_available_ms"] > at:
                            continue
                        try:
                            raw = json.loads(row["payload"])
                            if str(raw.get("gameId")) == str(contract.get("game_id")):
                                sporting = normalize(raw, row["receive_ms"], contract)
                                sporting.update(processed_ms=row["processed_ms"], strategy_available_ms=row["strategy_available_ms"])
                                break
                        except (ValueError, TypeError, AttributeError):
                            continue
                    received = observation["observed_ms"] if observation else None
                    processed = observation["processed_ms"] if observation else None
                    frames.append({"strategy_mode": "BOOK_DRIVEN", "time_ms": at, "strategy_available_ms": at,
                                   "received_ms": received, "processed_ms": processed,
                                   "backlog_ms": processed-received if processed is not None else None,
                                   "processing_assumption": "recorded callback processing completion; missing means UNKNOWN",
                                   "asset_id": asset, "market_id": market_id, "market": market,
                                   "market_observed_ms": version["observed_ms"], "market_available_ms": version["strategy_available_ms"],
                                   "book": observation["payload"] if observation else None,
                                   "book_ref": observation["ref"] if observation else None, "book_received_ms": received,
                                   "connected": bool(latest_health and latest_health["kind"] == "connected" and not gap),
                                   "health_available_ms": latest_health["strategy_available_ms"] if latest_health else None, "sports": sporting})
            trades = []
            for row in trade_buckets.get(at, []):
                p = json.loads(row["payload"])
                try:
                    trades.append({"event_id": row["event_key"], "asset_id": p["asset_id"], "condition_id": p["market"], "side": p["side"], "price": p["price"], "size": p["size"],
                                   "exchange_ms": int(p["timestamp"]), "received_ms": row["local_receive_timestamp"], "processed_ms": row["processed_ms"], "strategy_available_ms": at,
                                   "same_ms_ordering": "cross-channel UNKNOWN; no new-order same-ms fills"})
                except (KeyError, TypeError, ValueError):
                    unknown["trades"] += 1
            events.append({"id": digest(["book-input-v2.1", str(source), at, frames, trades]), "time_ms": at, "frames": frames, "trades": trades, "provenance": "public-recordings; processing-available; no odds"})
        return store.seal(events, {"mode": "BOOK_DRIVEN", "timing_version": "v2.1-processing-available", "source_kind": "public-recordings", "start_ms": start, "end_ms": end, "step_ms": step_ms,
                                   "unknown_processing_counts": unknown, "sports_contracts": contracts or {}, "network_calls": 0, "same_ms_policy": "permissions first, no same-ms new-order fills; ambiguity retained"})
    finally:
        db.close()
        if sports:
            sports.close()
