"""Event-sourced FIFO accounting. All movements are local paper cash flows."""
import copy
import json
from contextlib import nullcontext
from decimal import localcontext

from ..fair_value.maths import PRECISION, decimal
from ..fair_value.providers import reject_secrets
from ..storage import encode


def empty():
    return {"cash": "0", "capital": "0", "cash_flows": "0", "realized_trading_pnl": "0", "fees": "0", "rewards": "0",
            "settlement_cash_flow": "0", "pair_cash_flow": "0", "lots": [], "assets": {}, "settled_markets": {},
            "last_ms": -1, "cost_basis_complete": True, "closures": [], "hedge_intents": {}}


def balance(state, asset):
    return sum((decimal(l["remaining"]) for l in state["lots"] if l["asset_id"] == asset), decimal(0))


def close_lots(state, asset, qty, price, event, exit_fair=None, kind="SELL"):
    if balance(state, asset) < qty:
        raise ValueError("cannot exit more than held inventory")
    remaining = qty
    for lot in state["lots"]:
        if lot["asset_id"] != asset or decimal(lot["remaining"]) == 0:
            continue
        take = min(remaining, decimal(lot["remaining"]))
        cost = decimal(lot["price"])*take if lot["price"] is not None else None
        pnl = price*take-cost if cost is not None else None
        entry_fair = lot.get("context", {}).get("fair_probability")
        attribution = {"entry_edge": None, "spread_capture": None, "directional_residual": None, "additive_reconciliation": False,
                       "markout": "separate_diagnostic", "hedge_slippage": "separate_diagnostic", "stale_quote_loss": str(min(decimal(0), pnl)) if pnl is not None and lot.get("context", {}).get("stale_quote") else "0"}
        if cost is not None and entry_fair is not None and (exit_fair is not None or kind in {"SETTLEMENT", "PAIR"}):
            # Preserve exact reference cancellation; 42-digit fair * fractional quantity needs extra precision.
            with localcontext() as attribution_ctx:
                attribution_ctx.prec = PRECISION*3
                fair = decimal(entry_fair)
                reference = decimal(exit_fair) if exit_fair is not None else price
                attribution.update(entry_edge=str((fair-decimal(lot["price"]))*take),
                                   directional_residual=str((reference-fair)*take),
                                   spread_capture=str((price-reference)*take), additive_reconciliation=True,
                                   definition="entry reference edge + fair-value movement + exit premium to fair; fees excluded", decimal_precision=PRECISION*3)
                if sum(decimal(attribution[k]) for k in ("entry_edge", "directional_residual", "spread_capture")) != pnl:
                    raise ValueError("attribution reconciliation failed")
        closure = {"id": f"{event['id']}:{lot['id']}", "lot_id": lot["id"], "asset_id": asset, "market_id": lot["market_id"], "quantity": str(take),
                   "cost": str(cost) if cost is not None else None, "proceeds": str(price*take), "gross_pnl": str(pnl) if pnl is not None else None,
                   "entry_ms": lot["time_ms"], "exit_ms": event["time_ms"], "exit_kind": kind, "attribution": attribution}
        state["closures"].append(closure)
        if pnl is not None:
            state["realized_trading_pnl"] = str(decimal(state["realized_trading_pnl"])+pnl)
        lot["remaining"] = str(decimal(lot["remaining"])-take)
        remaining -= take
        if remaining == 0:
            break


def reduce_event(state, event):
    state = copy.deepcopy(state)
    p, kind, at = event["payload"], event["kind"], event["time_ms"]
    if type(at) is not int or at < state["last_ms"]:
        raise ValueError("accounting time must be nondecreasing")
    state["last_ms"] = at
    if kind == "CAPITAL":
        amount = decimal(p["amount"])
        if amount < 0:
            raise ValueError("negative capital contribution")
        state["cash"] = str(decimal(state["cash"])+amount)
        state["capital"] = str(decimal(state["capital"])+amount)
    elif kind == "ASSET":
        asset = p["asset_id"]
        meta = {k: p[k] for k in ("market_id", "role", "label", "complement")}
        if asset in state["assets"] and state["assets"][asset] != meta:
            raise ValueError("immutable binary asset mapping changed")
        if meta["role"] not in {"YES", "NO"} or not meta["complement"] or meta["complement"] == asset:
            raise ValueError("invalid binary asset mapping")
        state["assets"][asset] = meta
    elif kind in {"SEED", "FILL"}:
        asset, market = p["asset_id"], p["market_id"]
        old_asset = state["assets"].get(asset)
        if old_asset and old_asset["market_id"] != market:
            raise ValueError("asset cannot move between markets")
        meta = {"market_id": market, "role": p.get("role", old_asset["role"] if old_asset else "UNMAPPED"),
                "label": p.get("label", old_asset["label"] if old_asset else asset), "complement": p.get("complement", old_asset["complement"] if old_asset else None)}
        if old_asset and meta != old_asset:
            raise ValueError("fill cannot change immutable binary asset mapping")
        state["assets"][asset] = meta
        qty, fee = decimal(p["quantity"]), decimal(p.get("fee", "0"))
        price = decimal(p["price"]) if p.get("price") is not None else None
        if qty < 0 or (kind == "FILL" and qty == 0) or fee < 0 or (price is not None and not 0 <= price <= 1) or (kind == "FILL" and price is None):
            raise ValueError("invalid fill/seed amount")
        if market in state["settled_markets"]:
            raise ValueError("cannot trade after settlement")
        side = p.get("side", "BUY")
        if side not in {"BUY", "SELL"}:
            raise ValueError("invalid side")
        if p.get("purpose") == "HEDGE":
            hedge = state["hedge_intents"].get(p.get("hedge_intent_id"))
            if not hedge or side != "BUY" or asset != hedge["asset_id"] or decimal(hedge["filled"])+qty > decimal(hedge["quantity"]):
                raise ValueError("hedge fill requires a matching bounded paper hedge intent")
            hedge["filled"] = str(decimal(hedge["filled"])+qty)
        if kind == "SEED" or side == "BUY":
            state["lots"].append({**p, "id": event["id"], "remaining": str(qty), "time_ms": at, "seed": kind == "SEED"})
            if price is None and qty:
                state["cost_basis_complete"] = False
            if kind == "SEED":
                if price is not None:
                    state["capital"] = str(decimal(state["capital"])+qty*price)
            else:
                state["cash"] = str(decimal(state["cash"])-qty*price-fee)
                state["cash_flows"] = str(decimal(state["cash_flows"])-qty*price)
        else:
            close_lots(state, asset, qty, price, event, p.get("context", {}).get("fair_probability"))
            state["cash"] = str(decimal(state["cash"])+qty*price-fee)
            state["cash_flows"] = str(decimal(state["cash_flows"])+qty*price)
        state["fees"] = str(decimal(state["fees"])+fee)
    elif kind == "HEDGE_INTENT":
        lot = next((l for l in state["lots"] if l["id"] == p["lot_id"]), None)
        qty = decimal(p["quantity"])
        if not lot or qty <= 0 or qty > decimal(lot["remaining"]) or state["assets"][lot["asset_id"]].get("complement") != p["asset_id"]:
            raise ValueError("invalid bounded paper hedge intent")
        state["hedge_intents"][event["id"]] = {**p, "filled": "0", "time_ms": at, "research_only": True}
    elif kind == "REWARD":
        amount = decimal(p["amount"])
        if amount < 0 or not p.get("evidence") or not 0 <= p["evidence"].get("observed_ms", at+1) <= at:
            raise ValueError("paper rewards require nonnegative amount and as-of evidence")
        state["rewards"] = str(decimal(state["rewards"])+amount)
        state["cash"] = str(decimal(state["cash"])+amount)
    elif kind == "PAIR":
        yes, no, qty = p["yes_asset"], p["no_asset"], decimal(p["quantity"])
        if qty <= 0 or yes == no or yes not in state["assets"] or no not in state["assets"] or state["assets"][yes]["market_id"] != state["assets"][no]["market_id"] or state["assets"][yes].get("complement") != no:
            raise ValueError("invalid complete-share pair")
        if not p.get("evidence") or p["evidence"].get("observed_ms", at+1) > at:
            raise ValueError("paper pair cash flow requires as-of settlement contract evidence")
        # Split $1 proceeds evenly for attribution, not as a statement of individual outcome value.
        close_lots(state, yes, qty, decimal(".5"), event, kind="PAIR")
        close_lots(state, no, qty, decimal(".5"), event, kind="PAIR")
        fee = decimal(p.get("fee", "0"))
        if fee < 0:
            raise ValueError("negative pair fee")
        state["cash"] = str(decimal(state["cash"])+qty-fee)
        state["fees"] = str(decimal(state["fees"])+fee)
        state["pair_cash_flow"] = str(decimal(state["pair_cash_flow"])+qty)
    elif kind == "SETTLEMENT":
        market, payouts = p["market_id"], p["payouts"]
        assets = {a for a, meta in state["assets"].items() if meta["market_id"] == market}
        evidence = p.get("evidence", {})
        if market in state["settled_markets"] or set(payouts) != assets or len(assets) != 2 or any(not 0 <= decimal(v) <= 1 for v in payouts.values()) or sum(decimal(v) for v in payouts.values()) != 1 or not evidence or evidence.get("observed_ms", at+1) > at:
            raise ValueError("invalid/duplicate/future binary settlement evidence")
        cash = decimal(0)
        for asset in sorted(assets):
            qty = balance(state, asset)
            close_lots(state, asset, qty, decimal(payouts[asset]), event, kind="SETTLEMENT") if qty else None
            cash += qty*decimal(payouts[asset])
        state["cash"] = str(decimal(state["cash"])+cash)
        state["settlement_cash_flow"] = str(decimal(state["settlement_cash_flow"])+cash)
        state["settled_markets"][market] = {"time_ms": at, "payouts": payouts, "evidence": evidence}
    else:
        raise ValueError("unsupported accounting event")
    if decimal(state["cash"]) < 0:
        raise ValueError("paper account has insufficient cash including fees")
    return state


class Ledger:
    def __init__(self, db, run_id, lane):
        self.db, self.run_id, self.lane = db, run_id, lane

    def state(self):
        row = self.db.execute("SELECT payload FROM l6_accounts WHERE run_id=? AND lane=?", (self.run_id, self.lane)).fetchone()
        return json.loads(row[0]) if row else empty()

    def add(self, event_id, at, kind, payload):
        reject_secrets(payload)
        event = {"id": event_id, "time_ms": at, "kind": kind, "payload": payload}
        context = nullcontext() if self.db.in_transaction else self.db
        with context, localcontext() as ctx:
            ctx.prec = PRECISION
            old = self.db.execute("SELECT * FROM l6_events WHERE id=?", (event_id,)).fetchone()
            if old:
                if old["run_id"] != self.run_id or old["lane"] != self.lane or old["payload"] != encode(payload) or old["time_ms"] != at or old["kind"] != kind:
                    raise ValueError("accounting event ID reused with different content")
                return False
            before = self.state()
            after = reduce_event(before, event)
            self.db.execute("INSERT INTO l6_events(id,run_id,lane,time_ms,kind,payload) VALUES(?,?,?,?,?,?)", (event_id, self.run_id, self.lane, at, kind, encode(payload)))
            self.db.execute("INSERT INTO l6_accounts VALUES(?,?,?) ON CONFLICT(run_id,lane) DO UPDATE SET payload=excluded.payload", (self.run_id, self.lane, encode(after)))
            for closure in after["closures"][len(before["closures"]):]:
                self.db.execute("INSERT INTO l6_closures VALUES(?,?,?,?,?)", (closure["id"], self.run_id, self.lane, at, encode(closure)))
            return True

    def rebuild(self, at_ms=None):
        state = empty()
        with localcontext() as ctx:
            ctx.prec = PRECISION
            query = "SELECT * FROM l6_events WHERE run_id=? AND lane=?"
            params = [self.run_id, self.lane]
            if at_ms is not None:
                query += " AND time_ms<=?"
                params.append(at_ms)
            for r in self.db.execute(query+" ORDER BY seq", params):
                state = reduce_event(state, {"id": r["id"], "time_ms": r["time_ms"], "kind": r["kind"], "payload": json.loads(r["payload"])})
        return state

    def audit(self):
        if self.rebuild() != self.state():
            raise ValueError("FIFO accounting cache differs from immutable journal")
        return True


def valuation(state, marks, orders=()):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return _valuation(state, marks, orders)


def _valuation(state, marks, orders=()):
    missing = []
    market_value, unrealized = decimal(0), decimal(0)
    positions = {a: str(balance(state, a)) for a in state["assets"]}
    for lot in state["lots"]:
        qty = decimal(lot["remaining"])
        if not qty:
            continue
        mark = marks.get(lot["asset_id"])
        if mark is None:
            missing.append(lot["asset_id"])
            continue
        mark = decimal(mark)
        if not 0 <= mark <= 1:
            raise ValueError("invalid mark")
        market_value += qty*mark
        if lot["price"] is not None:
            unrealized += qty*(mark-decimal(lot["price"]))
    active = [o for o in orders if o["state"] in {"QUEUED", "SUBMITTING", "OPEN", "PARTIALLY_FILLED", "CANCEL_PENDING", "TIMEOUT", "UNKNOWN", "RECONCILED"}]
    reserved = sum((decimal(o["price"])*decimal(o.get("remaining", decimal(o["size"])-decimal(o.get("filled", "0")))) for o in active if o["side"] == "BUY"), decimal(0))
    units = sum((decimal(v) for v in positions.values()), decimal(0))+sum((decimal(o.get("remaining", decimal(o["size"])-decimal(o.get("filled", "0")))) for o in active if o["side"] == "BUY"), decimal(0))
    reserved_sell = {a: sum((decimal(o.get("remaining", decimal(o["size"])-decimal(o.get("filled", "0")))) for o in active if o["side"] == "SELL" and o.get("asset_id") == a), decimal(0)) for a in positions}
    reliable = not missing and state["cost_basis_complete"]
    equity = decimal(state["cash"])+market_value if not missing else None
    trading = decimal(state["realized_trading_pnl"])+unrealized if reliable else None
    net = trading-decimal(state["fees"])+decimal(state["rewards"]) if reliable else None
    if reliable and equity-decimal(state["capital"]) != net:
        raise ValueError("Net PnL does not reconcile to cash and positions")
    return {"cash": state["cash"], "capital_contributions": state["capital"], "positions": positions,
            "reserved_cash": str(reserved), "available_cash": str(decimal(state["cash"])-reserved), "cash_flows": state["cash_flows"],
            "reserved_inventory": {a: str(q) for a, q in reserved_sell.items()}, "available_inventory": {a: str(max(decimal(0), decimal(positions[a])-q)) for a, q in reserved_sell.items()},
            "realized_trading_pnl": state["realized_trading_pnl"] if state["cost_basis_complete"] else None,
            "unrealized_trading_pnl": str(unrealized) if reliable else None, "trading_pnl": str(trading) if trading is not None else None,
            "fees": state["fees"], "rewards": state["rewards"], "settlement_cash_flow": state["settlement_cash_flow"], "pair_cash_flow": state["pair_cash_flow"],
            "market_value": str(market_value) if not missing else None, "equity": str(equity) if equity is not None else None,
            "net_pnl": str(net) if net is not None else None, "reconciled": reliable, "missing_marks": sorted(set(missing)),
            "cost_basis_complete": state["cost_basis_complete"], "conservative_committed_exposure": str(units), "virtual": True, "not_real_profit": True}
