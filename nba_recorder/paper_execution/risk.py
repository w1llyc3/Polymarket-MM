"""Fail-closed paper risk checks, including committed (not only filled) exposure."""
from dataclasses import dataclass

from ..fair_value.maths import decimal
from ..quote_engine.model import Policy, hard_risk

ACTIVE = {"QUEUED", "SUBMITTING", "OPEN", "PARTIALLY_FILLED", "CANCEL_PENDING", "TIMEOUT", "UNKNOWN", "RECONCILED"}
TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}


@dataclass(frozen=True)
class ExecutionPolicy(Policy):
    request_timeout_ms: int = 100
    submit_ack_ms: int = 5
    cancel_latency_ms: int = 10
    risk_detection_ms: int = 1
    data_transport_ms: int = 2
    rest_latency_ms: int = 5
    rest_timeout_ms: int = 100
    max_price_jump: str = "0.05"
    max_daily_loss: str = "20"


def commitments(account, orders):
    buys = [o for o in orders if o["state"] in ACTIVE and o["side"] == "BUY"]
    sells = [o for o in orders if o["state"] in ACTIVE and o["side"] == "SELL"]
    cash = sum((decimal(o["price"])*(decimal(o["size"])-decimal(o["filled"])) for o in buys), decimal(0))
    units = sum((decimal(q) for q in account["positions"].values()), decimal(0)) + sum((decimal(o["size"])-decimal(o["filled"]) for o in buys), decimal(0))
    event_risk = {}
    for asset, qty in account["positions"].items():
        event = account["events"][asset]
        event_risk[event] = event_risk.get(event, decimal(0))+decimal(qty)
    for o in buys:
        event_risk[o["event_id"]] = event_risk.get(o["event_id"], decimal(0))+decimal(o["size"])-decimal(o["filled"])
    reserved_sell = {}
    for o in sells:
        reserved_sell[o["asset_id"]] = reserved_sell.get(o["asset_id"], decimal(0))+decimal(o["size"])-decimal(o["filled"])
    return {"reserved_cash": cash, "account_units": units, "event_units": event_risk, "reserved_sell": reserved_sell}


def order_risk(order, frame, account, orders, policy, at):
    if policy.strategy_mode == "BOOK_DRIVEN":
        from ..inplay.reference import submission_risk
        return submission_risk(order, frame, account, orders, policy, at)
    reasons = hard_risk(frame, policy)
    if at < frame["time_ms"] or at-frame["time_ms"] > frame.get("signal", {}).get("config", {}).get("book_max_age_ms", 0):
        reasons.append("decision_transport_stale_or_future")
    if any(o["state"] in {"UNKNOWN", "TIMEOUT", "RECONCILED"} for o in orders):
        reasons.append("unknown_order_state")
    try:
        price, size = decimal(order["price"]), decimal(order["size"])
        tick = decimal(frame["book"]["tick_size"])
        fair = decimal(frame["signal"]["fair_probability"])
        side = order["side"]
        config = frame["signal"]["config"]
        if not 0 <= at-frame["book_received_ms"] <= config["book_max_age_ms"]:
            reasons.append("book_stale_at_submission")
        if not 0 <= at-frame["signal"]["market_observed_ms"] <= config["metadata_max_age_ms"]:
            reasons.append("metadata_stale_at_submission")
        if any(not 0 <= at-s[k] <= config["odds_max_age_ms"] for s in frame["signal"]["sources"] if s.get("included") for k in ("updated_ms", "received_ms")):
            reasons.append("odds_stale_at_submission")
        if at >= frame["market"]["game_start_ms"]-policy.start_guard_ms:
            reasons.append("start_protection")
        if not 0 < decimal(order["min_edge"]) < 1:
            reasons.append("invalid_minimum_edge")
        if side not in {"BUY", "SELL"} or not tick <= price <= 1-tick or price % tick or size % decimal(".01") or size < decimal(frame["book"]["minimum_order_size"]):
            reasons.append("invalid_order_grid_or_size")
        if price*size > decimal(policy.max_order_notional):
            reasons.append("single_order_notional")
        if (side == "BUY" and price >= decimal(frame["book"]["ask"])) or (side == "SELL" and price <= decimal(frame["book"]["bid"])):
            reasons.append("maker_only_reject")
        if (fair-price if side == "BUY" else price-fair) < decimal(order["min_edge"]):
            reasons.append("edge_lost")
        budget = commitments(account, orders)
        if side == "BUY":
            if budget["reserved_cash"]+price*size > decimal(account["cash"]):
                reasons.append("insufficient_unreserved_cash")
            if budget["account_units"]+size > decimal(policy.max_account_risk):
                reasons.append("account_committed_risk")
            if budget["event_units"].get(order["event_id"], decimal(0))+size > decimal(policy.max_event_risk):
                reasons.append("event_committed_risk")
            existing = sum((decimal(o["size"])-decimal(o["filled"]) for o in orders if o["state"] in ACTIVE and o["side"] == "BUY" and o["asset_id"] == order["asset_id"]), decimal(0))
            if decimal(account["positions"].get(order["asset_id"], "0"))+existing+size > decimal(policy.max_asset_inventory):
                reasons.append("asset_inventory_limit")
        elif size+budget["reserved_sell"].get(order["asset_id"], decimal(0)) > decimal(account["positions"].get(order["asset_id"], "0")):
            reasons.append("insufficient_unreserved_inventory")
        if at >= order["expires_ms"]:
            reasons.append("intent_expired")
    except (ValueError, KeyError, TypeError, ArithmeticError):
        reasons.append("invalid_order_or_reference")
    return sorted(set(reasons))
