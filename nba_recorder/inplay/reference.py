"""Book reference is not a fair probability, prediction, or outcome edge."""
from decimal import localcontext
from ..fair_value.maths import PRECISION, decimal
from ..quote_engine.model import TICKS, grid
from ..paper_execution.risk import ACTIVE, commitments


def observed_mid(frame, policy, at):
    if book_data_risk(frame, policy, at):
        return None
    try:
        return str((decimal(frame["book"]["bid"])+decimal(frame["book"]["ask"]))/2)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return None


def book_data_risk(frame, policy, at=None):
    """Fresh book/identity/transport proof, independent of quote permission."""
    reasons = []
    at = frame.get("time_ms", -1) if at is None else at
    try:
        if frame.get("strategy_mode") != "BOOK_DRIVEN":
            reasons.append("strategy_mode_mismatch")
        m, b = frame["market"], frame["book"]
        if m.get("venue") != "polymarket.com" or m.get("league") != "nba" or m.get("market_type") != "moneyline":
            reasons.append("unsupported_market")
        status = m["status"]
        if not all(status.get(k) for k in ("active", "enableOrderBook", "acceptingOrders")) or any(status.get(k) for k in ("closed", "archived", "tracking_state")) or m.get("event_ended"):
            reasons.append("abnormal_market_state")
        ids = {o["asset_id"] for o in m["outcomes"]}
        if len(ids) != 2 or frame["asset_id"] not in ids or b["asset_id"] != frame["asset_id"] or b["market_id"] != frame["market_id"] or m["market_id"] != frame["market_id"] or not m.get("condition_id"):
            reasons.append("identity_mismatch")
        if frame.get("connected") is not True:
            reasons.append("disconnected")
        for stamp, age in ((frame["book_received_ms"], policy.book_max_age_ms), (frame["time_ms"], policy.book_max_age_ms), (frame["market_observed_ms"], policy.metadata_max_age_ms)):
            if not 0 <= at-stamp <= age:
                reasons.append("stale_or_future_data")
        if b.get("exchange_timestamp") is not None and b["exchange_timestamp"] > at:
            reasons.append("future_exchange_timestamp")
        if frame["processed_ms"] < frame["received_ms"] or frame["processed_ms"] > at or frame["processed_ms"]-frame["received_ms"] > policy.max_backlog_ms or frame.get("backlog_ms", 0) > policy.max_backlog_ms:
            reasons.append("processing_backlog")
        bid, ask, tick = decimal(b["bid"]), decimal(b["ask"]), decimal(b["tick_size"])
        if not 0 < bid < ask < 1 or tick not in {decimal(t) for t in TICKS} or decimal(b["minimum_order_size"]) <= 0:
            reasons.append("invalid_book_prices_grid_or_size")
        for levels, best in ((b["bids"], bid), (b["asks"], ask)):
            if not levels or decimal(levels[0]["price"]) != best or any(not 0 < decimal(l["price"]) < 1 or decimal(l["size"]) <= 0 for l in levels):
                reasons.append("invalid_depth")
            prices = [decimal(level["price"]) for level in levels]
            if len(set(prices)) != len(prices) or prices != sorted(prices, reverse=levels is b["bids"]):
                reasons.append("unsorted_or_duplicate_depth")
            if policy.enhanced and decimal(levels[0]["size"]) < decimal(policy.min_top_depth):
                reasons.append("depth_drained")
        reasons.extend(b.get("flags", []))
    except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError):
        reasons.append("missing_or_invalid_book_reference")
    return sorted(set(reasons))


def book_risk(frame, policy, at=None, *, check_operator=True):
    reasons = book_data_risk(frame, policy, at)
    at = frame.get("time_ms", -1) if at is None else at
    if check_operator and frame.get("runtime_permission", {}).get("quotes_enabled") is False:
        reasons.append("operator_quotes_disabled")
    try:
        m = frame["market"]
        if m.get("discovery_schema") and not m.get("paper_scope_approved"):
            reasons.append("competition_paper_scope_not_approved")
        if m.get("season_type") == "UNKNOWN" or (m.get("season_type") == "PRESEASON" and not m.get("rules_verified")):
            reasons.append("season_or_rules_unverified")
        if frame.get("phase") not in {"ACTIVE_MM", "REDUCE_ONLY"}:
            reasons.append("phase_not_permitted")
        if policy.allow_manual_phase:
            from .manual import effective
            manual_state, errors = effective(frame.get("manual_confirmation"), m, at, policy)
            reasons.extend(errors)
            if manual_state not in {"ACTIVE_MM", "REDUCE_ONLY"} or (manual_state == "REDUCE_ONLY" and frame.get("phase") != "REDUCE_ONLY"):
                reasons.append("manual_phase_not_permitted")
            return sorted(set(reasons))
        sports = frame.get("sports") or {}
        if sports.get("event_id") != m["event_id"] or not sports.get("identity_evidence") or sports.get("status") != "InProgress" or sports.get("live") is not True or sports.get("ended") or sports.get("paused") is not False or sports.get("conflict") or sports.get("clock_semantics") != "REMAINING_IN_PERIOD" or not sports.get("clock_evidence"):
            reasons.append("unreliable_sports_state")
        for key in ("received_ms", "observed_ms"):
            if type(sports.get(key)) is not int or not 0 <= at-sports[key] <= policy.sports_max_age_ms:
                reasons.append("sports_stale_or_future")
        period, remaining = sports.get("period"), sports.get("remaining_s")
        if type(period) is not int or type(remaining) is not int or period < 1 or not 0 <= remaining <= (720 if period <= 4 else 300):
            reasons.append("invalid_clock")
        elif period >= 4 and remaining <= policy.close_remaining_s:
            reasons.append("late_close_threshold")
        elif (period > 4 or (period == 4 and remaining <= policy.reduce_remaining_s)) and frame.get("phase") != "REDUCE_ONLY":
            reasons.append("late_reduce_boundary")
    except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError):
        reasons.append("missing_or_invalid_book_reference")
    return sorted(set(reasons))


def reduction_capacity(order, frame, account, orders):
    """No reverse imbalance or dismantling paired shares, including commitments."""
    assets = [o["asset_id"] for o in frame["market"]["outcomes"]]
    own = order["asset_id"]
    if own not in assets:
        return decimal(0)
    other = next(a for a in assets if a != own)
    delta = decimal(account["positions"].get(own, "0"))-decimal(account["positions"].get(other, "0"))
    improves = (order["side"] == "SELL" and delta > 0) or (order["side"] == "BUY" and delta < 0)
    if not improves:
        return decimal(0)
    excess_asset = own if delta > 0 else other
    committed = sum((decimal(o["size"])-decimal(o["filled"]) for o in orders if o["state"] in ACTIVE and o["asset_id"] in assets and ((o["side"] == "SELL" and o["asset_id"] == excess_asset) or (o["side"] == "BUY" and o["asset_id"] != excess_asset))), decimal(0))
    return max(decimal(0), abs(delta)-committed)


def submission_risk(order, frame, account, orders, policy, at, retaining=False):
    reasons = book_risk(frame, policy, at)
    try:
        price, qty = decimal(order["price"]), decimal(order["size"])
        book, asset = frame["book"], order["asset_id"]
        tick = decimal(book["tick_size"])
        side = order["side"]
        size_invalid = qty <= 0 or (not retaining and (qty < decimal(book["minimum_order_size"]) or qty % decimal(".01")))
        if side not in {"BUY", "SELL"} or not tick <= price <= 1-tick or price % tick or size_invalid or order.get("maker_only") is not True:
            reasons.append("unsafe_order_grid_size_or_postonly")
        if asset != frame["asset_id"] or order["market_id"] != frame["market_id"] or order["event_id"] != frame["market"]["event_id"]:
            reasons.append("intent_identity_mismatch")
        if qty > decimal(policy.max_order_shares) or qty*price > decimal(policy.max_order_notional):
            reasons.append("order_shares_or_dollars_limit")
        if (side == "BUY" and price >= decimal(book["ask"])) or (side == "SELL" and price <= decimal(book["bid"])):
            reasons.append("maker_only_reject")
        mid = (decimal(book["bid"])+decimal(book["ask"]))/2
        if order.get("purpose") == "RISK_EXIT":
            if frame["phase"] != "REDUCE_ONLY" or qty > reduction_capacity(order, frame, account, orders) or price*qty > decimal(policy.exit_notional) or abs(price-mid) > decimal(policy.exit_slippage):
                reasons.append("exit_budget_slippage_or_portfolio_risk")
        else:
            distance = mid-price if side == "BUY" else price-mid
            if distance < decimal(policy.quote_distance):
                reasons.append("quote_distance_lost")
            if frame["phase"] == "REDUCE_ONLY":
                reasons.append("entry_forbidden_in_reduce_only")
        budget = commitments(account, orders)
        if any(o["state"] in {"UNKNOWN", "TIMEOUT", "RECONCILED"} for o in orders):
            reasons.append("unknown_order_state")
        if side == "BUY":
            if budget["reserved_cash"]+price*qty > decimal(account["cash"]):
                reasons.append("insufficient_unreserved_cash")
            if budget["account_units"]+qty > decimal(policy.max_account_risk) or budget["event_units"].get(order["event_id"], decimal(0))+qty > decimal(policy.max_event_risk):
                reasons.append("committed_risk_limit")
            pending = sum((decimal(o["size"])-decimal(o["filled"]) for o in orders if o["state"] in ACTIVE and o["asset_id"] == asset and o["side"] == "BUY"), decimal(0))
            if decimal(account["positions"].get(asset, "0"))+pending+qty > decimal(policy.max_asset_inventory):
                reasons.append("inventory_limit")
        elif qty+budget["reserved_sell"].get(asset, decimal(0)) > decimal(account["positions"].get(asset, "0")):
            reasons.append("insufficient_unreserved_inventory")
        if at >= order["expires_ms"]:
            reasons.append("intent_expired")
    except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError):
        reasons.append("invalid_order")
    return sorted(set(reasons))


def book_targets(frame, account, orders, catalog, policy):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        reasons = book_risk(frame, policy)
        result = {"strategy_mode": "BOOK_DRIVEN", "state": "NO_QUOTE", "targets": [], "reasons": reasons,
                  "book_ref": frame.get("book_ref"), "phase": frame.get("phase"), "virtual": True}
        if reasons:
            return result
        book, asset, at = frame["book"], frame["asset_id"], frame["time_ms"]
        mid = (decimal(book["bid"])+decimal(book["ask"]))/2
        inventory = decimal(account["positions"].get(asset, "0"))
        complement = next(o["asset_id"] for o in frame["market"]["outcomes"] if o["asset_id"] != asset)
        paired = min(inventory, decimal(account["positions"].get(complement, "0")))
        directional = inventory-decimal(account["positions"].get(complement, "0"))
        # Deliberate one-sided entry suppression, not a claim of two-sided skew.
        center = mid-decimal(policy.inventory_skew)*max(decimal(0), directional) if policy.enhanced else mid
        distance, tick = decimal(policy.quote_distance), decimal(book["tick_size"])
        result.update(reference_mid=str(mid), quote_center=str(center), quote_distance=str(distance), inventory=str(inventory), side_reasons={})
        result.update(paired_inventory=str(paired), net_directional_inventory=str(directional), inventory_rule="SUPPRESS_BUY_ON_NET_LONG; ask distance floor unchanged")
        result["actions"] = []
        for side in ("BUY", "SELL"):
            reduce = frame["phase"] == "REDUCE_ONLY"
            price = grid(min(center-distance, mid-distance, decimal(book["ask"])-tick), tick, side) if side == "BUY" else grid(max(center+distance, mid+distance, decimal(book["bid"])+tick), tick, side)
            if reduce:
                # Independent risk-exit distance budget; still post-only.
                price = decimal(book["bid"]) if side == "BUY" else decimal(book["ask"])
            retained = None
            for existing in orders:
                if existing["asset_id"] != asset or existing["side"] != side or existing["state"] not in {"SUBMITTING", "OPEN", "PARTIALLY_FILLED"}:
                    continue
                purpose = "RISK_EXIT" if reduce else "MAKER"
                if decimal(existing["price"]) != price or existing.get("purpose") != purpose:
                    continue
                remaining = decimal(existing["size"])-decimal(existing["filled"])
                probe = {**existing, "size": str(remaining), "filled": "0"}
                others = [o for o in orders if o["id"] != existing["id"]]
                if not submission_risk(probe, frame, account, others, policy, at, retaining=True):
                    retained = {**probe, "action": "RETAIN", "retained_order_id": existing["id"]}
                    break
            if retained:
                result["targets"].append(retained)
                result["actions"].append({"action": "RETAIN", "order_id": retained["retained_order_id"], "side": side})
                continue
            qty = min(decimal(policy.order_size), decimal(policy.max_order_shares))
            if price > 0:
                qty = min(qty, decimal(policy.max_order_notional)/price)
            candidate = {"asset_id": asset, "market_id": frame["market_id"], "event_id": frame["market"]["event_id"], "side": side, "price": str(price), "size": str(qty),
                         "purpose": "RISK_EXIT" if reduce else "MAKER", "strategy_mode": "BOOK_DRIVEN", "maker_only": True,
                         "expires_ms": min(at+policy.ttl_ms, frame["book_received_ms"]+policy.book_max_age_ms,
                                           frame["manual_confirmation"]["expires_ms"] if policy.allow_manual_phase else frame["sports"]["received_ms"]+policy.sports_max_age_ms)}
            if reduce:
                qty = min(qty, reduction_capacity(candidate, frame, account, orders), decimal(policy.exit_notional)/price if price > 0 else decimal(0))
            if side == "SELL":
                qty = min(qty, max(decimal(0), inventory-commitments(account, orders)["reserved_sell"].get(asset, decimal(0))))
            candidate["size"] = str(grid(qty, decimal(".01"), "BUY"))
            errors = submission_risk(candidate, frame, account, orders, policy, at)
            if errors:
                result["side_reasons"][side] = errors
            else:
                candidate["action"] = "CREATE"
                result["targets"].append(candidate)
        retained_ids = {t["retained_order_id"] for t in result["targets"] if t.get("action") == "RETAIN"}
        for existing in orders:
            if existing["asset_id"] == asset and existing["state"] in ACTIVE and existing["id"] not in retained_ids:
                result["actions"].append({"action": "WAIT" if existing["state"] in {"CANCEL_PENDING", "UNKNOWN", "TIMEOUT", "RECONCILED"} else "CANCEL", "order_id": existing["id"], "side": existing["side"]})
        sides = {t["side"] for t in result["targets"]}
        result["state"] = "TWO_SIDED" if len(sides) == 2 else "BUY_ONLY" if sides == {"BUY"} else "SELL_ONLY" if sides else "NO_QUOTE"
        return result
