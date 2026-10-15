"""Research-only full-depth comparison. No order target or execution is produced."""
from ..fair_value.maths import decimal


def sweep(levels, qty, buying):
    remaining, value = qty, decimal(0)
    for level in sorted(levels, key=lambda x: decimal(x["price"]), reverse=not buying):
        price, size = decimal(level["price"]), decimal(level["size"])
        if not 0 < price < 1 or size < 0:
            raise ValueError("invalid executable depth")
        take = min(size, remaining)
        remaining -= take
        value += take*price
        if remaining == 0:
            return str(value)
    return None


def evaluate_hedge(market, held_asset, qty, holdings, books, at_ms, max_age_ms=30000, fee_rate="0", fair_probability=None, available_cash=None):
    """Complement lock is conditional on normal binary full payout; cancellation may differ."""
    result = {"status": "NO_RESEARCH", "research_only": True, "auto_execute": False, "real_submission_calls": 0, "reasons": [], "plans": []}
    try:
        assets = [o["asset_id"] for o in market["outcomes"]]
        qty, fee = decimal(qty), decimal(fee_rate)
        if len(assets) != 2 or len(set(assets)) != 2 or held_asset not in assets or market["venue"] != "polymarket.com" or not market["status"].get("active") or market["status"].get("closed") or not market["status"].get("acceptingOrders") or not market["status"].get("enableOrderBook") or market.get("event_live") or at_ms >= market["game_start_ms"]:
            raise ValueError("invalid binary pregame market")
        if qty <= 0 or qty > decimal(holdings.get(held_asset, "0")) or not 0 <= fee < 1:
            raise ValueError("invalid holdings/fee assumption")
        other = next(a for a in assets if a != held_asset)
        for asset in assets:
            book = books[asset]
            if book["asset_id"] != asset or book["market_id"] != market["market_id"] or book.get("flags") or book.get("connected") is not True or not 0 <= at_ms-book["local_receive_timestamp"] <= max_age_ms or (book.get("exchange_timestamp") is not None and book["exchange_timestamp"] > at_ms) or not 0 <= decimal(book["bid"]) < decimal(book["ask"]) <= 1:
                raise ValueError("unreliable or mismatched executable book")
        direct = sweep(books[held_asset]["bids"], qty, False) if qty >= decimal(books[held_asset]["minimum_order_size"]) else None
        complement = sweep(books[other]["asks"], qty, True) if qty >= decimal(books[other]["minimum_order_size"]) else None
        result["plans"] = [
            {"kind": "DIRECT_CLOSE", "executable_depth": direct is not None, "net_future_value": str(decimal(direct)*(1-fee)) if direct is not None else None},
            {"kind": "BUY_COMPLEMENT", "executable_depth": complement is not None, "cash_required": str(decimal(complement)*(1+fee)) if complement is not None else None,
             "conditional_paired_future_value": str(qty-decimal(complement)*(1+fee)) if complement is not None else None,
             "cash_feasible": decimal(available_cash) >= decimal(complement)*(1+fee) if available_cash is not None and complement is not None else None,
             "assumption": "combined terminal payout $1 per pair; no merge/split execution; cancellation rules must be checked"},
            {"kind": "HOLD", "expected_future_value": str(qty*decimal(fair_probability)) if fair_probability is not None and 0 < decimal(fair_probability) < 1 else None, "worst_future_value": "0", "unhedged": True}]
        result.update(status="RESEARCH", fee_assumption=str(fee), comparison_excludes_sunk_cost=True, evidence={"market": market, "books": books, "at_ms": at_ms}, limitations=["fees supplied as an assumption, not fetched", "depth can disappear", "hold value is probabilistic", "not realized profit"])
    except (ValueError, KeyError, TypeError, ArithmeticError) as error:
        result["reasons"] = [str(error)]
    return result
