from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, localcontext

from ..fair_value.maths import PRECISION, decimal

TICKS = {"0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"}
OPEN = {"OPEN", "PARTIALLY_FILLED"}
TERMINAL = {"FILLED", "CANCELLED", "EXPIRED"}
TRANSITIONS = {"CREATED": {"OPEN"}, "OPEN": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "EXPIRED"},
               "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "EXPIRED"},
               "CANCEL_PENDING": {"CANCELLED"}, **{s: set() for s in TERMINAL}}


@dataclass(frozen=True)
class Policy:
    strategy_mode: str = "EXTERNAL_FAIR"
    initial_cash: str = "1000"
    initial_inventory: str = "0"
    target_inventory: str = "0"
    order_size: str = "5"
    max_order_notional: str = "25"
    max_event_risk: str = "100"
    max_account_risk: str = "500"
    max_asset_inventory: str = "50"
    min_spread: str = "0.02"
    inventory_skew: str = "0.001"
    ttl_ms: int = 30000
    start_guard_ms: int = 300000
    markout_window_ms: int = 5000

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key == "strategy_mode":
                if value not in {"EXTERNAL_FAIR", "BOOK_DRIVEN"}:
                    raise ValueError("explicit supported strategy_mode required")
            elif isinstance(value, bool):
                continue
            elif key.endswith("_ms"):
                if type(value) is not int or value <= 0:
                    raise ValueError("policy durations must be positive integer milliseconds")
            elif decimal(value) < 0:
                raise ValueError("policy values must be finite/nonnegative")
        for key in ("order_size", "max_order_notional", "max_event_risk", "max_account_risk", "max_asset_inventory", "min_spread"):
            if decimal(getattr(self, key)) <= 0:
                raise ValueError("size/risk/spread limits must be positive")


def grid(price, tick, side):
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR if side == "BUY" else ROUND_CEILING) * tick


def hard_risk(frame, policy):
    if policy.strategy_mode == "BOOK_DRIVEN":
        from ..inplay.reference import book_risk
        return book_risk(frame, policy)
    if frame.get("strategy_mode", "EXTERNAL_FAIR") != "EXTERNAL_FAIR":
        return ["strategy_mode_mismatch"]
    reasons = []
    at, market, signal, book = frame["time_ms"], frame.get("market"), frame.get("signal", {}), frame.get("book")
    if not market or not book:
        return ["missing_market_or_book"]
    status = market.get("status", {})
    if market.get("venue") != "polymarket.com" or market.get("league") != "nba" or market.get("market_type") != "moneyline":
        reasons.append("unsupported_market")
    if not status.get("active") or status.get("closed") or status.get("archived") or not status.get("enableOrderBook") or not status.get("acceptingOrders") or status.get("tracking_state") or market.get("event_live") or market.get("event_ended"):
        reasons.append("abnormal_market_state")
    if not isinstance(market.get("game_start_ms"), int) or at >= market["game_start_ms"] - policy.start_guard_ms:
        reasons.append("start_protection")
    if frame.get("connected") is not True:
        reasons.append("disconnected")
    if signal.get("status") != "RESEARCH_SIGNAL" or not signal.get("reference_eligible"):
        reasons.append("invalid_fair_reference")
    included = [s for s in signal.get("sources", []) if s.get("included")]
    if len({s.get("source_id") for s in included if s.get("source_id")}) < 2:
        reasons.append("insufficient_independent_sources")
    config = signal.get("config", {})
    try:
        if signal["decision_ms"] != at:
            reasons.append("signal_time_mismatch")
        if not 0 <= at-signal["market_observed_ms"] <= config["metadata_max_age_ms"]:
            reasons.append("metadata_stale_or_future")
        book_time = book.get("book_receive_timestamp", frame["book_received_ms"])
        if not 0 <= at-book_time <= config["book_max_age_ms"]:
            reasons.append("book_stale_or_future")
        if book.get("exchange_timestamp") is not None and book["exchange_timestamp"] > at:
            reasons.append("future_book")
        for source in included:
            if not 0 <= at-source["updated_ms"] <= config["odds_max_age_ms"] or not 0 <= at-source["received_ms"] <= config["odds_max_age_ms"]:
                reasons.append("odds_stale_or_future")
        fair = decimal(signal["fair_probability"])
        bid, ask, tick, minimum = decimal(book["bid"]), decimal(book["ask"]), decimal(book["tick_size"]), decimal(book["minimum_order_size"])
        if not 0 < fair < 1 or not 0 <= bid < ask <= 1 or minimum <= 0:
            reasons.append("invalid_prices_or_size")
        if tick not in {decimal(t) for t in TICKS}:
            reasons.append("unsupported_tick_size")
        if book.get("flags"):
            reasons.extend(book["flags"])
        if frame.get("asset_id") != book.get("asset_id") or frame.get("market_id") != book.get("market_id") or signal.get("asset_id") != frame.get("asset_id") or signal.get("market_id") != frame.get("market_id"):
            reasons.append("identity_mismatch")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        reasons.append("missing_or_invalid_reference_fields")
    return sorted(set(reasons))


def resources(account, orders, catalog, excluding_asset=None):
    working = [o for o in orders if o["state"] in OPEN and o["asset_id"] != excluding_asset]
    buys = [o for o in working if o["side"] == "BUY"]
    sells = [o for o in working if o["side"] == "SELL"]
    reserved_cash = sum((decimal(o["price"])*decimal(o["remaining"]) for o in buys), decimal(0))
    risk = sum((decimal(q) for q in account["positions"].values()), decimal(0)) + sum((decimal(o["remaining"]) for o in buys), decimal(0))
    event_risk = {}
    for asset, q in account["positions"].items():
        event = catalog[asset]["event_id"]
        event_risk[event] = event_risk.get(event, decimal(0)) + decimal(q)
    for o in buys:
        event = catalog[o["asset_id"]]["event_id"]
        event_risk[event] = event_risk.get(event, decimal(0)) + decimal(o["remaining"])
    return {"free_cash": decimal(account["cash"]) - reserved_cash, "risk": risk, "event_risk": event_risk,
            "reserved_sell": {a: sum((decimal(o["remaining"]) for o in sells if o["asset_id"] == a), decimal(0)) for a in catalog},
            "working_buy": {a: sum((decimal(o["remaining"]) for o in buys if o["asset_id"] == a), decimal(0)) for a in catalog}}


def targets(frame, account, orders, catalog, min_edge, policy=Policy()):
    if policy.strategy_mode == "BOOK_DRIVEN":
        from ..inplay.reference import book_targets
        return book_targets(frame, account, orders, catalog, policy)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        reasons = hard_risk(frame, policy)
        result = {"time_ms": frame["time_ms"], "market_id": frame["market_id"], "asset_id": frame["asset_id"],
                  "state": "NO_QUOTE", "reasons": reasons, "side_reasons": {}, "targets": [], "min_edge": str(min_edge),
                  "cash": account["cash"], "inventory": account["positions"].get(frame["asset_id"], "0"),
                  "fair_probability": frame.get("signal", {}).get("fair_probability"), "book_ref": frame.get("book_ref"),
                  "source_refs": frame.get("signal", {}).get("sources", []), "signal_id": frame.get("signal", {}).get("id"),
                  "real_submission_calls": 0, "record_only": True}
        if reasons:
            return result
        book, signal, market = frame["book"], frame["signal"], frame["market"]
        tick, minimum, fair = decimal(book["tick_size"]), decimal(book["minimum_order_size"]), decimal(signal["fair_probability"])
        bid, ask, edge = decimal(book["bid"]), decimal(book["ask"]), decimal(min_edge)
        inventory = decimal(result["inventory"])
        center = fair - decimal(policy.inventory_skew)*(inventory-decimal(policy.target_inventory))
        half = max(decimal(policy.min_spread), ask-bid)/2
        price_buy = grid(min(center-half, fair-edge, bid+tick, ask-tick, 1-tick), tick, "BUY")
        price_sell = grid(max(center+half, fair+edge, ask-tick, bid+tick, tick), tick, "SELL")
        budget = resources(account, orders, catalog, excluding_asset=frame["asset_id"])
        event = catalog[frame["asset_id"]]["event_id"]
        expiry = min(frame["time_ms"]+policy.ttl_ms, market["game_start_ms"]-policy.start_guard_ms,
                     frame["book_received_ms"]+signal["config"]["book_max_age_ms"],
                     signal["market_observed_ms"]+signal["config"]["metadata_max_age_ms"],
                     *(s[k]+signal["config"]["odds_max_age_ms"] for s in signal["sources"] if s["included"] for k in ("received_ms", "updated_ms")))
        for side, price in (("BUY", price_buy), ("SELL", price_sell)):
            side_reasons = []
            if not tick <= price <= 1-tick or price % tick:
                side_reasons.append("unsafe_price_or_tick")
            if (side == "BUY" and price >= ask) or (side == "SELL" and price <= bid):
                side_reasons.append("maker_only_reject")
            gross_edge = fair-price if side == "BUY" else price-fair
            if gross_edge < edge:
                side_reasons.append("insufficient_gross_edge")
            qty = decimal(policy.order_size)
            if price > 0:
                qty = min(qty, decimal(policy.max_order_notional)/price)
                if side == "BUY":
                    qty = min(qty, max(decimal(0), budget["free_cash"])/price,
                              max(decimal(0), decimal(policy.max_event_risk)-budget["event_risk"].get(event, decimal(0))),
                              max(decimal(0), decimal(policy.max_account_risk)-budget["risk"]),
                              max(decimal(0), decimal(policy.max_asset_inventory)-inventory-budget["working_buy"].get(frame["asset_id"], decimal(0))))
                else:
                    qty = min(qty, max(decimal(0), inventory-budget["reserved_sell"].get(frame["asset_id"], decimal(0))))
            qty = grid(qty, decimal("0.01"), "BUY")
            if qty < minimum:
                side_reasons.append("balance_inventory_or_risk_below_min_size")
            if expiry <= frame["time_ms"]:
                side_reasons.append("reference_expired")
            if side_reasons:
                result["side_reasons"][side] = side_reasons
            else:
                result["targets"].append({"side": side, "price": str(price), "size": str(qty), "gross_edge": str(gross_edge),
                                           "tick_size": str(tick), "minimum_order_size": str(minimum), "expires_ms": expiry,
                                           "maker_only": True, "virtual": True})
        sides = {t["side"] for t in result["targets"]}
        result["state"] = "TWO_SIDED" if len(sides) == 2 else "BUY_ONLY" if "BUY" in sides else "SELL_ONLY" if "SELL" in sides else "NO_QUOTE"
        if not sides:
            result["reasons"] = ["no_feasible_target"]
        result.update(reservation_price=str(center), observed_spread=str(ask-bid), inventory_skew=policy.inventory_skew)
        return result
