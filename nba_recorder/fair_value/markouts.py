import json
from decimal import localcontext

from ..storage import encode
from .engine import Config, book_quality, books, market_at
from .maths import PRECISION, decimal

HORIZONS = (10, 30, 60, 300)


def markout(db, candidate, horizon_s, evaluated_ms):
    target = candidate["decision_ms"] + horizon_s * 1000
    result = {"candidate_id": candidate["id"], "horizon_s": horizon_s, "target_ms": target, "evaluated_ms": evaluated_ms,
              "hypothetical": True, "executed": False, "not_profit": True, "fees_included": False,
              "fill_assumption": "conditional_on_fill_at_candidate_price; no fill/queue model", "status": "PENDING"}
    if evaluated_ms < target:
        return result
    version = market_at(db, candidate["market_id"], target)
    if not version or target-version["observed_ms"] > candidate.get("metadata_max_age_ms", 360000) or version["market"]["game_start_ms"] <= target or version["market"].get("status", {}).get("tracking_state") or version["market"].get("status", {}).get("closed") or version["market"].get("event_live"):
        result.update(status="UNAVAILABLE", reason="not_pregame_or_missing_metadata")
        return result
    window = candidate["markout_window_ms"]
    points = books(db, candidate["market_id"], candidate["asset_id"], start=target, end=min(target + window, evaluated_ms))
    if not points:
        if evaluated_ms >= target + window:
            result.update(status="UNAVAILABLE", reason="no_observation_within_window")
        return result
    # First observation after target; never search past a bad observation for a better one.
    point = points[0]
    config = Config(book_max_age_ms=candidate["book_max_age_ms"])
    reasons = book_quality(db, point, point["observed_ms"], config)
    observed_version = market_at(db, candidate["market_id"], point["observed_ms"])
    if not observed_version or point["observed_ms"]-observed_version["observed_ms"] > candidate.get("metadata_max_age_ms", 360000) or observed_version["market"]["game_start_ms"] <= point["observed_ms"] or observed_version["market"].get("status", {}).get("tracking_state") or observed_version["market"].get("status", {}).get("closed") or observed_version["market"].get("event_live"):
        reasons.append("not_pregame_at_observation")
    if reasons:
        result.update(status="UNAVAILABLE", reason="invalid_target_observation", flags=sorted(set(reasons)), book_ref=point["ref"])
        return result
    with localcontext() as ctx:
        ctx.prec = PRECISION
        bid, ask, quote = decimal(point["payload"]["bid"]), decimal(point["payload"]["ask"]), decimal(candidate["quote_price"])
        mid = (bid + ask) / 2
        signed_mid = mid - quote if candidate["side"] == "BUY" else quote - mid
        liquidation = bid - quote if candidate["side"] == "BUY" else quote - ask
    result.update(status="OBSERVED", book_ref=point["ref"], snapshot_id=point["snapshot_id"], event_key=point["event_key"],
                  observed_ms=point["observed_ms"], delay_ms=point["observed_ms"]-target,
                  mid_markout=str(signed_mid), gross_liquidation_markout=str(liquidation))
    return result


def update_markouts(store, evaluated_ms):
    records = []
    for row in store.db.execute("SELECT payload FROM fv_candidates WHERE decision_ms<=? ORDER BY decision_ms,id", (evaluated_ms,)).fetchall():
        candidate = json.loads(row["payload"])
        for horizon in HORIZONS:
            previous = store.db.execute("SELECT status,payload FROM fv_markouts WHERE candidate_id=? AND horizon_s=?", (candidate["id"], horizon)).fetchone()
            if previous and json.loads(previous["payload"])["evaluated_ms"] > evaluated_ms:
                # Historical queries must not reveal previously evaluated future outcomes.
                records.append(markout(store.db, candidate, horizon, evaluated_ms))
                continue
            if previous and previous["status"] != "PENDING":
                records.append(json.loads(previous["payload"]))
                continue
            result = markout(store.db, candidate, horizon, evaluated_ms)
            with store.db:
                store.db.execute("INSERT INTO fv_markouts VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,horizon_s) DO UPDATE SET status=excluded.status,evaluated_ms=excluded.evaluated_ms,payload=excluded.payload", (candidate["id"], horizon, result["status"], evaluated_ms, encode(result)))
            records.append(result)
    return records
