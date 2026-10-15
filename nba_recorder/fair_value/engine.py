import json
from dataclasses import asdict, dataclass
from decimal import localcontext

from ..teams import team_code
from .matcher import match_event
from .maths import PRECISION, consensus, decimal, proportional_devig
from .store import digest


@dataclass(frozen=True)
class Config:
    odds_max_age_ms: int = 120000
    book_max_age_ms: int = 30000
    metadata_max_age_ms: int = 360000
    start_tolerance_ms: int = 0
    max_source_range: str = "0.05"
    min_edge: str = "0.01"
    markout_window_ms: int = 5000

    def __post_init__(self):
        if any(type(x) is not int or x <= 0 for x in (self.odds_max_age_ms, self.book_max_age_ms, self.metadata_max_age_ms, self.markout_window_ms)):
            raise ValueError("age limits/windows must be positive integer milliseconds")
        if type(self.start_tolerance_ms) is not int or self.start_tolerance_ms < 0:
            raise ValueError("start tolerance must be nonnegative")
        if not 0 < decimal(self.max_source_range) <= 1 or not 0 <= decimal(self.min_edge) <= 1:
            raise ValueError("invalid conflict range/minimum edge")


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def market_at(db, market_id, at_ms):
    if not table_exists(db, "market_versions"):
        return None
    row = db.execute("SELECT * FROM market_versions WHERE market_id=? AND observed_ms<=? ORDER BY observed_ms DESC,id DESC LIMIT 1", (market_id, at_ms)).fetchone()
    return {"ref": row["id"], "observed_ms": row["observed_ms"], "market": json.loads(row["metadata"])} if row else None


def books(db, market_id, asset, start=None, end=None):
    result = []
    if table_exists(db, "book_observations"):
        query = "SELECT * FROM book_observations WHERE market_id=? AND asset_id=?"
        params = [market_id, asset]
        if start is not None:
            query += " AND received_ms>=?"
            params.append(start)
        if end is not None:
            query += " AND received_ms<=?"
            params.append(end)
        for r in db.execute(query, params):
            result.append({"ref": "observation:" + str(r["id"]), "snapshot_id": r["snapshot_id"],
                           "event_key": r["event_key"], "observed_ms": r["received_ms"], "payload": json.loads(r["payload"])})
    # Legacy immutable snapshots are useful but never fabricate repeat confirmations.
    if table_exists(db, "snapshots"):
        query = "SELECT * FROM snapshots WHERE market_id=? AND asset_id=?"
        params = [market_id, asset]
        if start is not None:
            query += " AND local_receive_timestamp>=?"
            params.append(start)
        if end is not None:
            query += " AND local_receive_timestamp<=?"
            params.append(end)
        represented = {r["snapshot_id"] for r in result}
        for r in db.execute(query, params):
            if r["id"] not in represented:
                result.append({"ref": "snapshot:" + str(r["id"]), "snapshot_id": r["id"], "event_key": r["event_key"],
                               "observed_ms": r["local_receive_timestamp"], "payload": json.loads(r["payload"])})
    return sorted(result, key=lambda r: (r["observed_ms"], r["ref"].startswith("observation:"), int(r["ref"].split(":")[1])))


def book_quality(db, book, at_ms, config):
    if not book:
        return ["no_asof_book"]
    s = book["payload"]
    reasons = list(s.get("flags", []))
    try:
        received = int(s.get("book_receive_timestamp", book["observed_ms"]))
        if not 0 <= at_ms - received <= config.book_max_age_ms:
            reasons.append("book_stale_or_future")
        if s.get("exchange_timestamp") is not None and int(s["exchange_timestamp"]) > at_ms:
            reasons.append("future_exchange_timestamp")
        bid, ask = decimal(s["bid"]), decimal(s["ask"])
        if not 0 <= bid < ask <= 1:
            reasons.append("invalid_locked_or_crossed_bbo")
    except (KeyError, ValueError, TypeError):
        reasons.append("invalid_or_empty_book")
    phase = db.execute("SELECT id,kind FROM health WHERE local_timestamp<=? AND kind IN ('connected','disconnected','stopped') ORDER BY local_timestamp DESC,id DESC LIMIT 1", (at_ms,)).fetchone()
    if not phase or phase["kind"] != "connected":
        reasons.append("no_connected_session_asof")
    return sorted(set(reasons))


def sources_at(db, market, at_ms, config):
    if not table_exists(db, "fv_odds"):
        return []
    batches = {}
    for row in db.execute("SELECT * FROM fv_raw_odds WHERE received_ms<=? ORDER BY received_ms,rowid", (at_ms,)):
        batches[row["provider"]] = row["id"]
    latest = {}
    for row in db.execute("SELECT * FROM fv_odds WHERE received_ms<=? ORDER BY received_ms,id", (at_ms,)):
        latest[(row["provider"], row["source_id"], row["event_id"])] = row
    entries = []
    for _, r in sorted(latest.items()):
        q = json.loads(r["payload"])
        reasons = match_event(market, q, at_ms, config.start_tolerance_ms)
        if r["raw_id"] != batches.get(r["provider"]):
            reasons.append("not_in_latest_provider_snapshot")
        if not r["source_id"] or not r["event_id"] or r["source_id"] == "None" or r["event_id"] == "None":
            reasons.append("missing_source_or_event_identity")
        if q.get("outcome_count") != 2:
            reasons.append("not_two_way_odds")
        if r["data_kind"] == "synthetic" and not market.get("synthetic"):
            reasons.append("synthetic_odds_for_real_market")
        age = at_ms - r["updated_ms"] if r["updated_ms"] is not None else None
        if age is None or isinstance(q.get("updated_ms"), bool) or not 0 <= age <= config.odds_max_age_ms or at_ms - r["received_ms"] > config.odds_max_age_ms:
            reasons.append("missing_stale_or_future_odds")
        fair = None
        try:
            fair = proportional_devig(q.get("home_odds"), q.get("away_odds"), q.get("odds_format"))
        except ValueError:
            reasons.append("invalid_odds")
        entries.append({"odds_id": r["id"], "raw_odds_id": r["raw_id"], "provider": r["provider"], "source_id": r["source_id"],
                        "event_id": r["event_id"], "data_kind": r["data_kind"], "home_odds": q.get("home_odds"),
                        "away_odds": q.get("away_odds"), "odds_format": q.get("odds_format"), "updated_ms": r["updated_ms"],
                        "received_ms": r["received_ms"], "age_ms": age, "fair": fair, "included": not reasons,
                        "exclusions": sorted(set(reasons))})
        entries[-1]["event_status"] = q.get("status")
    # A fresh, identity-matched cancellation/live report vetoes the whole event.
    veto = any(e["event_status"] in {"cancelled", "canceled", "live", "ended", "live_or_started", "suspended"}
               and not set(e["exclusions"]) & {"team_or_home_away_mismatch", "start_time_mismatch", "league_or_season_mismatch", "missing_start_time", "missing_stale_or_future_odds", "not_in_latest_provider_snapshot"}
               for e in entries)
    if veto:
        for e in entries:
            e["included"] = False
            e["exclusions"].append("source_event_not_pregame")
    # Same underlying sportsbook through multiple providers counts only once.
    by_source = {}
    for e in entries:
        if e["included"]:
            by_source.setdefault(e["source_id"], []).append(e)
    for group in by_source.values():
        if any(len({e["event_id"] for e in group if e["provider"] == p}) > 1 for p in {e["provider"] for e in group}):
            for e in group:
                e["included"] = False
                e["exclusions"].append("ambiguous_source_event")
            continue
        winner = sorted(group, key=lambda e: (-e["updated_ms"], -e["received_ms"], e["provider"], e["odds_id"]))[0]
        for e in group:
            if e is not winner:
                e["included"] = False
                e["exclusions"].append("duplicate_underlying_source")
    return entries


def evaluate(db, market_id, at_ms, config=Config()):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        version = market_at(db, market_id, at_ms)
        market = version["market"] if version else None
        # Missing old metadata is a dependency failure, never use today's metadata.
        if market is None or not market.get("outcomes"):
            signals = [{"market_id": market_id, "asset_id": None, "decision_ms": at_ms, "status": "NO_QUOTE",
                        "reasons": ["missing_asof_market_metadata"], "sources": [], "candidates": []}]
        else:
            sources = sources_at(db, market, at_ms, config)
            included = [s for s in sources if s["included"]]
            agg = consensus([s["fair"]["home"] for s in included]) if included else None
            for entry in sources:
                entry["deviation_from_consensus_home"] = str(decimal(entry["fair"]["home"]) - decimal(agg["home"])) if agg and entry["fair"] else None
            reference_age = max(s["age_ms"] for s in included) if included else None
            signals = []
            for outcome in market.get("outcomes", []):
                observations = books(db, market_id, outcome["asset_id"], end=at_ms)
                book = observations[-1] if observations else None
                reasons = book_quality(db, book, at_ms, config)
                if at_ms - version["observed_ms"] > config.metadata_max_age_ms:
                    reasons.append("market_metadata_stale")
                if not agg:
                    reasons.append("no_valid_reference")
                elif decimal(agg["range"]) > decimal(config.max_source_range):
                    reasons.append("source_conflict")
                side = team_code(outcome["label"])
                home, away = team_code(market.get("home_team")), team_code(market.get("away_team"))
                fair = decimal(agg["home" if side == home else "away"]) if agg and side in {home, away} and side else None
                if not home or not away or home == away or side not in {home, away}:
                    reasons.append("unverified_asset_team_mapping")
                age_factor = max(decimal(0), 1 - decimal(reference_age) / config.odds_max_age_ms) if reference_age is not None else decimal(0)
                dispersion_factor = max(decimal(0), 1 - decimal(agg["stddev"]) / decimal(config.max_source_range)) if agg else decimal(0)
                confidence = min(decimal(len(included)) / 3, decimal(1)) * age_factor * dispersion_factor
                if len(included) < 2:
                    confidence = min(confidence, decimal("0.35"))
                data_kind = "synthetic" if market.get("synthetic") else "observed"
                if data_kind == "synthetic":
                    confidence = min(confidence, decimal("0.25"))
                if reasons:
                    confidence = decimal(0)
                s = {"market_id": market_id, "asset_id": outcome["asset_id"], "outcome": outcome["label"], "decision_ms": at_ms,
                     "status": "NO_QUOTE" if reasons else "RESEARCH_SIGNAL" if len(included) >= 2 else "RESEARCH_ONLY",
                     "reasons": sorted(set(reasons)), "stage": "record_only", "data_kind": data_kind,
                     "auto_live_quote_allowed": False, "reference_eligible": not reasons and len(included) >= 2,
                     "market_version_id": version["ref"], "market_observed_ms": version["observed_ms"],
                     "book_ref": book["ref"] if book else None, "snapshot_id": book["snapshot_id"] if book else None,
                     "book_event_key": book["event_key"] if book else None, "book_observed_ms": book["observed_ms"] if book else None,
                     "bid": book["payload"].get("bid") if book else None, "ask": book["payload"].get("ask") if book else None,
                     "sources": sources, "source_count": len(included), "consensus": agg, "fair_probability": str(fair) if fair is not None else None,
                     "reference_age_ms": reference_age, "signal_confidence": str(confidence), "confidence_calibrated": False,
                     "buy_gross_edge": None, "sell_gross_edge": None, "buy_taker_gross_edge": None, "sell_taker_gross_edge": None, "consensus_deviation": None, "candidates": []}
                if not reasons:
                    bid, ask = decimal(book["payload"]["bid"]), decimal(book["payload"]["ask"])
                    s.update(buy_gross_edge=str(fair - bid), sell_gross_edge=str(ask - fair),
                             buy_taker_gross_edge=str(fair - ask), sell_taker_gross_edge=str(bid - fair), consensus_deviation=str(fair - (bid + ask) / 2))
                signals.append(s)
        for s in signals:
            s.setdefault("auto_live_quote_allowed", False)
            s.setdefault("reference_eligible", False)
            s.setdefault("stage", "record_only")
            s["config"] = asdict(config)
            s["engine_version"] = "lesson3-v1"
            s["id"] = digest(s)
            if s["status"] != "NO_QUOTE":
                book = next(b for b in books(db, s["market_id"], s["asset_id"], end=at_ms) if b["ref"] == s["book_ref"])
                for side, edge, price in [("BUY", s["buy_gross_edge"], book["payload"]["bid"]), ("SELL", s["sell_gross_edge"], book["payload"]["ask"])]:
                    if decimal(edge) >= decimal(config.min_edge):
                        candidate = {"signal_id": s["id"], "market_id": s["market_id"], "asset_id": s["asset_id"], "decision_ms": at_ms,
                                     "side": side, "quote_price": price, "gross_edge": edge, "book_ref": s["book_ref"],
                                     "market_version_id": s["market_version_id"], "raw_odds_ids": sorted({x["raw_odds_id"] for x in s["sources"] if x["included"]}),
                                     "hypothetical": True, "executed": False, "data_kind": s["data_kind"], "auto_live_quote_allowed": False,
                                     "reference_eligible": s["reference_eligible"], "markout_window_ms": config.markout_window_ms,
                                     "book_max_age_ms": config.book_max_age_ms, "metadata_max_age_ms": config.metadata_max_age_ms}
                        candidate["id"] = digest(candidate)
                        s["candidates"].append(candidate)
        return signals


def market_ids_at(db, at_ms):
    ids = set()
    if table_exists(db, "market_versions"):
        ids.update(r[0] for r in db.execute("SELECT DISTINCT market_id FROM market_versions WHERE observed_ms<=?", (at_ms,)))
    if table_exists(db, "snapshots"):
        ids.update(r[0] for r in db.execute("SELECT DISTINCT market_id FROM snapshots WHERE local_receive_timestamp<=?", (at_ms,)))
    return sorted(ids)
