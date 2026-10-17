"""Exit candidates for each remaining FIFO lot. Never submits orders or cash operations."""
from decimal import ROUND_HALF_UP, localcontext

from ..fair_value.maths import PRECISION, decimal
from ..fair_value.store import digest
from ..fair_value.matcher import polymarket_rules
from .ledger import balance


def executable(levels, quantity, buy, fee_rate):
    left, gross, fee = quantity, decimal(0), decimal(0)
    fills = []
    for level in sorted(levels, key=lambda x: decimal(x["price"]), reverse=not buy):
        price, size = decimal(level["price"]), decimal(level["size"])
        if not 0 < price < 1 or size < 0:
            raise ValueError("invalid depth")
        take = min(left, size)
        if take:
            gross += take*price
            if fee_rate is not None:
                # Official price-shaped taker formula, configured rate, cash-fee approximation.
                fee += (take*decimal(fee_rate)*price*(1-price)).quantize(decimal(".00001"), rounding=ROUND_HALF_UP)
            fills.append({"price": str(price), "quantity": str(take)})
            left -= take
        if not left:
            break
    return {"requested": str(quantity), "executable_quantity": str(quantity-left), "unfilled": str(left), "gross_cash": str(gross),
            "fee": str(fee) if fee_rate is not None else None, "full_depth_available": left == 0, "levels": fills}


def book_reasons(frame, at):
    if not frame or not frame.get("book"):
        return ["missing_book"]
    b = frame["book"]
    try:
        reasons = list(b.get("flags", []))
        if not frame.get("connected"):
            reasons.append("disconnected")
        age = at-b.get("book_receive_timestamp", frame["book_received_ms"])
        max_age=frame.get('signal',{}).get('config',{}).get('book_max_age_ms',frame.get('book_max_age_ms',3000))
        if not 0 <= age <= max_age or (b.get("exchange_timestamp") is not None and b["exchange_timestamp"] > at):
            reasons.append("book_stale_or_future")
        if not 0 <= decimal(b["bid"]) < decimal(b["ask"]) <= 1:
            reasons.append("invalid_bbo")
        m, status = frame["market"], frame["market"]["status"]
        if m["venue"] != "polymarket.com" or not status.get("active") or status.get("closed") or status.get("archived") or not status.get("acceptingOrders") or not status.get("enableOrderBook") or status.get("tracking_state"):
            reasons.append("market_unavailable")
        if b["asset_id"] != frame["asset_id"] or b["market_id"] != frame["market_id"]:
            reasons.append("book_identity_mismatch")
        return sorted(set(reasons))
    except (KeyError, ValueError, TypeError):
        return ["invalid_book_context"]


def evaluate_lot(lot, state, frames, at, config, reserved_sell="0", available_cash=None):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        asset, qty = lot["asset_id"], decimal(lot["remaining"])
        meta = state["assets"][asset]
        complement = meta.get("complement")
        frame, other = frames.get(asset), frames.get(complement)
        base = {"lot_id": lot["id"], "asset_id": asset, "market_id": lot["market_id"], "quantity": str(qty), "time_ms": at,
                "research_only": True, "auto_execute": False, "real_submission_calls": 0, "fee_source": config.fee_source}
        reasons = book_reasons(frame, at)
        paired_reasons = reasons+book_reasons(other, at)
        if not complement or not other or other["market_id"] != lot["market_id"]:
            paired_reasons.append("complement_not_verified")
        if frame and other:
            ids = {o["asset_id"] for o in frame["market"].get("outcomes", [])}
            other_ids = {o["asset_id"] for o in other["market"].get("outcomes", [])}
            if ids != {asset, complement} or other_ids != ids or frame["market"].get("condition_id") != other["market"].get("condition_id"):
                paired_reasons.append("binary_contract_identity_mismatch")
            if polymarket_rules(frame["market"]) is None or polymarket_rules(frame["market"]) != polymarket_rules(other["market"]):
                paired_reasons.append("unverified_complete_share_settlement_rules")
        available_qty = max(decimal(0), qty-decimal(reserved_sell))
        paired_remaining = min(balance(state, asset), balance(state, complement)) if complement else decimal(0)
        existing_paired = decimal(0)
        for prior_lot in state["lots"]:
            if prior_lot["asset_id"] != asset:
                continue
            allocation = min(decimal(prior_lot["remaining"]), paired_remaining)
            paired_remaining -= allocation
            if prior_lot["id"] == lot["id"]:
                existing_paired = allocation
                break
        reserved_unpaired = max(decimal(0), decimal(reserved_sell)-existing_paired)
        new_complement_needed = max(decimal(0), qty-existing_paired-reserved_unpaired)
        candidates = []
        for plan, blocked in (("EXIT", reasons), ("HEDGE", paired_reasons), ("HOLD", [])):
            p = {**base, "plan": plan, "status": plan+"_CANDIDATE", "reasons": sorted(set(blocked)), "book_refs": [f["book_ref"] for f in (frame, other) if f], "fee_assumption": config.taker_fee_rate}
            if plan == "HOLD":
                fair = frame.get("signal", {}).get("fair_probability") if frame else None
                valid = frame and frame.get('signal',{}).get("reference_eligible") and not reasons and frame.get('signal',{}).get("decision_ms") == at
                p.update(expected_future_value=str(qty*decimal(fair)) if valid and fair is not None else None, liquidity_required_now="0", immediate_fee="0", worst_terminal_value="0", unhedged=True,
                         assumptions=["future exit liquidity unknown", "settlement rules and delay still apply", "fair value is a reference, not realized value"])
            elif not blocked:
                try:
                    selected = frame if plan == "EXIT" else other
                    use_qty = available_qty if plan == "EXIT" else new_complement_needed
                    if plan == "HEDGE":
                        p.update(existing_paired_quantity=str(existing_paired), additional_complement_required=str(new_complement_needed), pairing_basis="FIFO allocation of already held complementary inventory")
                        if use_qty == 0:
                            residual = max(decimal(0), qty-existing_paired)
                            p.update(status="HOLD_CANDIDATE" if residual == 0 else "MANUAL_REVIEW", cash_required="0", paired_quantity="0", unhedged_residual_inventory=str(residual), no_additional_hedge_needed=residual == 0)
                            if residual:
                                p["reasons"].append("pending_exit_reserves_unpaired_inventory")
                            p["id"] = digest([base, plan, p["book_refs"], config.__dict__])
                            candidates.append(p)
                            continue
                    if use_qty < decimal(selected["book"]["minimum_order_size"]):
                        p["reasons"].append("available_quantity_below_minimum")
                    depth = executable(selected["book"]["bids" if plan == "EXIT" else "asks"], use_qty, plan == "HEDGE", config.taker_fee_rate)
                    p.update(depth=depth, available_inventory=str(available_qty), evidence={"book": selected["book"], "received_ms": selected["book_received_ms"]})
                    if not depth["full_depth_available"]:
                        p["reasons"].append("insufficient_depth_for_full_lot")
                    if config.taker_fee_rate is None:
                        p["reasons"].append("unknown_taker_fee")
                    else:
                        gross, fee = decimal(depth["gross_cash"]), decimal(depth["fee"])
                        executed_qty = decimal(depth["executable_quantity"])
                        if plan == "EXIT":
                            p.update(net_cash_received=str(gross-fee), estimated_gross_realized_pnl=str(gross-decimal(lot["price"])*executed_qty) if lot["price"] is not None else None,
                                     residual_inventory=str(qty-executed_qty), residual_value_uncertain=executed_qty < qty)
                        else:
                            cash = gross+fee
                            p.update(cash_required=str(cash), conditional_complete_pair_value=str(executed_qty-cash), paired_quantity=str(executed_qty), unhedged_residual_inventory=str(max(decimal(0), qty-existing_paired-executed_qty)),
                                     residual_value_uncertain=existing_paired+executed_qty < qty, assumptions=["same binary market pair pays $1 combined", "pair value covers newly executable portion only, not previously held pairs", "no automatic redemption/merge", "cash fee approximation and delay risk"],
                                     estimated_hedge_slippage_per_share=str(gross/executed_qty-decimal(lot["context"]["expected_hedge_price"])) if executed_qty and lot.get("context", {}).get("expected_hedge_price") is not None else None)
                            if available_cash is None or decimal(available_cash) < cash:
                                p["reasons"].append("cash_unavailable_or_insufficient")
                except (ValueError, TypeError, KeyError):
                    p["reasons"].append("invalid_execution_depth")
            if p["reasons"]:
                p["status"] = "MANUAL_REVIEW"
            p["id"] = digest([base, plan, p["book_refs"], config.__dict__])
            candidates.append(p)
        return candidates
