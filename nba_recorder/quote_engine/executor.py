"""Deterministic virtual execution. No network, SDK, signing, or order submission."""
import json
from dataclasses import asdict
from decimal import localcontext

from ..storage import encode
from ..fair_value.maths import PRECISION, decimal
from ..fair_value.store import digest
from .model import OPEN, TRANSITIONS, Policy, hard_risk, targets

LANES = [(f"{c}c-{proxy}", str(decimal(c)/100), proxy) for c in (1, 2, 3) for proxy in ("L0", "L1")]
HORIZONS = (10, 30, 60, 300)


class Executor:
    lanes = LANES
    version = "virtual-v2"
    def __init__(self, store, tape_id, policy=Policy()):
        self.store, self.db, self.policy = store, store.db, policy
        self.manifest = store.verify(tape_id)
        self.tape_id = tape_id
        self.run_id = digest([tape_id, asdict(policy), self.version])
        self.catalog = {}
        with self.db:
            fresh = self.db.execute("INSERT OR IGNORE INTO qe_runs VALUES(?,?,?,0,'PAUSED',0)", (self.run_id, tape_id, encode(asdict(policy)))).rowcount
            if fresh:
                for lane, _, _ in self.lanes:
                    account = {"cash": policy.initial_cash, "positions": {}}
                    self.db.execute("INSERT INTO qe_accounts VALUES(?,?,?)", (self.run_id, lane, encode(account)))
                    self.db.execute("INSERT INTO qe_journal(run_id,lane,input_seq,time_ms,kind,payload) VALUES(?,?,0,?,'SEED',?)", (self.run_id, lane, self.manifest["start_ms"], encode({"account": account, "virtual": True})))
        cursor = self.db.execute("SELECT cursor FROM qe_runs WHERE id=?", (self.run_id,)).fetchone()[0]
        for row in self.db.execute("SELECT payload FROM qe_input WHERE tape_id=? AND seq<=? ORDER BY seq", (tape_id, cursor)):
            for f in json.loads(row[0])["frames"]:
                self.catalog[f["asset_id"]] = {"event_id": f["market"]["event_id"]}
        self.audit()

    def account(self, lane):
        return json.loads(self.db.execute("SELECT payload FROM qe_accounts WHERE run_id=? AND lane=?", (self.run_id, lane)).fetchone()[0])

    def orders(self, lane):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM qe_orders WHERE run_id=? AND lane=? ORDER BY id", (self.run_id, lane))]

    def journal(self, lane, seq, at, kind, order=None, reason=None, evidence=None):
        account = self.account(lane)
        payload = {"order": order, "account": account, "reason": reason, "evidence": evidence, "virtual": True}
        self.db.execute("INSERT INTO qe_journal(run_id,lane,input_seq,time_ms,kind,order_id,payload) VALUES(?,?,?,?,?,?,?)", (self.run_id, lane, seq, at, kind, order["id"] if order else None, encode(payload)))
        if order:
            self.db.execute("INSERT INTO qe_orders VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,payload=excluded.payload", (order["id"], self.run_id, lane, order["asset_id"], order["state"], encode(order)))

    def transition(self, lane, seq, at, order, state, reason, evidence=None):
        if state not in TRANSITIONS[order["state"]]:
            raise ValueError("invalid virtual order transition")
        order["state"], order["updated_ms"] = state, at
        self.journal(lane, seq, at, state, order.copy(), reason, evidence)

    def cancel(self, lane, seq, at, order, reason):
        self.transition(lane, seq, at, order, "CANCEL_PENDING", reason)
        self.transition(lane, seq, at, order, "CANCELLED", reason)

    def anchor(self, lane, anchor, kind, at, asset, side, price, evidence):
        for horizon in HORIZONS:
            payload = {"anchor_ms": at, "asset_id": asset, "side": side, "price": price, "target_ms": at+horizon*1000,
                       "evidence": evidence, "virtual": True, "not_realized_profit": True}
            self.db.execute("INSERT OR IGNORE INTO qe_marks VALUES(?,?,?,?,?,'PENDING',?)", (anchor, kind, horizon, self.run_id, lane, encode(payload)))

    def fill(self, lane, seq, at, order, qty, evidence):
        account = self.account(lane)
        price, qty = decimal(order["price"]), min(decimal(order["remaining"]), qty)
        if qty <= 0 or order["state"] not in OPEN:
            return decimal(0)
        asset, buying = order["asset_id"], order["side"] == "BUY"
        holdings = decimal(account["positions"][asset])
        if (buying and decimal(account["cash"]) < price*qty) or (not buying and holdings < qty):
            raise ValueError("virtual balance invariant failed")
        account["cash"] = str(decimal(account["cash"]) + (-1 if buying else 1)*price*qty)
        account["positions"][asset] = str(holdings + (qty if buying else -qty))
        self.db.execute("UPDATE qe_accounts SET payload=? WHERE run_id=? AND lane=?", (encode(account), self.run_id, lane))
        order["remaining"] = str(decimal(order["remaining"])-qty)
        self.transition(lane, seq, at, order, "FILLED" if decimal(order["remaining"]) == 0 else "PARTIALLY_FILLED", "hypothetical_proxy_fill", {**evidence, "size": str(qty), "price": str(price)})
        self.anchor(lane, digest([order["id"], seq, evidence]), "HYPOTHETICAL_FILL", at, asset, order["side"], str(price), evidence)
        return qty

    def proxy_fills(self, lane, proxy, seq, event, frames):
        at = event["time_ms"]
        budgets = {}
        trades = []
        if proxy == "L1":
            for trade in event["trades"]:
                if self.db.execute("INSERT OR IGNORE INTO qe_consumed_trades VALUES(?,?,?,?)", (self.run_id, lane, trade["event_key"], trade["index"])).rowcount:
                    trades.append(trade)
        for order in sorted(self.orders(lane), key=lambda o: (o["created_ms"], o["id"])):
            frame = frames.get(order["asset_id"])
            if order["state"] not in OPEN or order["created_ms"] >= at or not frame or hard_risk(frame, self.policy):
                continue
            if proxy == "L0":
                if order.get("last_touch_ref") == frame["book_ref"]:
                    continue
                order["last_touch_ref"] = frame["book_ref"]
                book = frame["book"]
                level = "asks" if order["side"] == "BUY" else "bids"
                try:
                    price = decimal(book["ask" if order["side"] == "BUY" else "bid"])
                    size = sum((decimal(x["size"]) for x in book[level] if decimal(x["price"]) == price), decimal(0))
                    touched = price <= decimal(order["price"]) if order["side"] == "BUY" else price >= decimal(order["price"])
                    key = (frame["book_ref"], level)
                    budgets.setdefault(key, max(decimal(0), size))
                except (ValueError, KeyError, TypeError):
                    continue
                if touched:
                    used = self.fill(lane, seq, at, order, budgets[key], {"proxy": "L0_TOUCH", "book_ref": frame["book_ref"]})
                    budgets[key] -= used
                    if used == 0:
                        self.journal(lane, seq, at, "OBSERVATION", order.copy(), "touch_without_available_size")
                else:
                    self.journal(lane, seq, at, "OBSERVATION", order.copy(), "new_book_no_touch")
            else:
                for trade in trades:
                    p = trade["payload"]
                    try:
                        price, size = decimal(p["price"]), decimal(p["size"])
                        if p["asset_id"] != order["asset_id"] or p.get("market") != frame["market"]["condition_id"] or not 0 < price < 1 or size <= 0 or not 0 <= at-int(p["timestamp"]) <= frame["signal"]["config"]["book_max_age_ms"]:
                            continue
                        through = (order["side"] == "BUY" and p["side"] == "SELL" and price < decimal(order["price"])) or (order["side"] == "SELL" and p["side"] == "BUY" and price > decimal(order["price"]))
                        key = (trade["event_key"], trade["index"])
                        budgets.setdefault(key, size)
                    except (ValueError, TypeError, KeyError):
                        continue
                    if through:
                        budgets[key] -= self.fill(lane, seq, at, order, budgets[key], {"proxy": "L1_TRADE_THROUGH", "trade": trade})

    def markouts(self, event):
        at = event["time_ms"]
        frames = {f["asset_id"]: f for f in event["frames"]}
        rows = self.db.execute("SELECT * FROM qe_marks WHERE run_id=? AND status='PENDING'", (self.run_id,)).fetchall()
        for row in rows:
            p = json.loads(row["payload"])
            if at < p["target_ms"]:
                continue
            frame = frames.get(p["asset_id"])
            status = None
            if frame and frame["book_received_ms"] is not None and frame["book_received_ms"] >= p["target_ms"]:
                if at > p["target_ms"]+self.policy.markout_window_ms or hard_risk(frame, self.policy):
                    status = "UNAVAILABLE"
                else:
                    book = frame["book"]
                    mid = (decimal(book["bid"])+decimal(book["ask"]))/2
                    p.update(mid=str(mid), markout=str((mid-decimal(p["price"]))*(1 if p["side"] == "BUY" else -1)), book_ref=frame["book_ref"], observed_ms=frame["book_received_ms"])
                    status = "OBSERVED"
            elif at >= p["target_ms"]+self.policy.markout_window_ms:
                status = "UNAVAILABLE"
            if status:
                p["evaluated_ms"] = at
                self.db.execute("UPDATE qe_marks SET status=?,payload=? WHERE anchor_id=? AND kind=? AND horizon_s=?", (status, encode(p), row["anchor_id"], row["kind"], row["horizon_s"]))

    def step(self, row):
        event, seq = json.loads(row["payload"]), row["seq"]
        at, frames = event["time_ms"], {f["asset_id"]: f for f in event["frames"]}
        for f in event["frames"]:
            self.catalog[f["asset_id"]] = {"event_id": f["market"]["event_id"]}
        for lane, edge, proxy in self.lanes:
            account = self.account(lane)
            new_assets = [a for a in self.catalog if a not in account["positions"]]
            if new_assets:
                for asset in new_assets:
                    account["positions"][asset] = self.policy.initial_inventory
                self.db.execute("UPDATE qe_accounts SET payload=? WHERE run_id=? AND lane=?", (encode(account), self.run_id, lane))
                self.journal(lane, seq, at, "ASSET_SEED", reason="first_asof_asset_discovery", evidence={"assets": new_assets, "virtual_seed_per_asset": self.policy.initial_inventory})
            for order in self.orders(lane):
                if order["state"] not in OPEN:
                    continue
                f = frames.get(order["asset_id"])
                reasons = hard_risk(f, self.policy) if f else ["missing_frame"]
                if at >= order["expires_ms"]:
                    self.transition(lane, seq, at, order, "EXPIRED", "ttl_or_reference_expiry")
                elif reasons or decimal(order["tick_size"]) != decimal(f["book"]["tick_size"]) or decimal(order["minimum_order_size"]) != decimal(f["book"]["minimum_order_size"]):
                    self.cancel(lane, seq, at, order, reasons or ["market_grid_changed"])
            self.proxy_fills(lane, proxy, seq, event, frames)
            for f in event["frames"]:
                decision = targets(f, self.account(lane), self.orders(lane), self.catalog, edge, self.policy)
                decision_id = digest([self.run_id, lane, seq, f["asset_id"]])
                decision.update(id=decision_id, lane=lane, input_seq=seq, book=f["book"], metadata=f["market"], fair_signal=f["signal"])
                self.db.execute("INSERT INTO qe_decisions VALUES(?,?,?,?,?,?)", (decision_id, self.run_id, lane, seq, at, encode(decision)))
                working = [o for o in self.orders(lane) if o["asset_id"] == f["asset_id"] and o["state"] in OPEN]
                desired = {t["side"]: t for t in decision["targets"]}
                for old in working:
                    target = desired.get(old["side"])
                    if target and decimal(old["price"]) == decimal(target["price"]) and decimal(old["remaining"]) == decimal(target["size"]):
                        desired.pop(old["side"])
                    else:
                        self.cancel(lane, seq, at, old, "cancel_replace" if target else decision["reasons"] or decision["side_reasons"])
                        if target:
                            self.journal(lane, seq, at, "MODIFY", old.copy(), "replace_loses_assumed_priority", {"replacement_decision": decision_id})
                for target in desired.values():
                    order = {**target, "id": digest([decision_id, target["side"]]), "asset_id": f["asset_id"], "market_id": f["market_id"], "decision_id": decision_id,
                             "remaining": target["size"], "state": "CREATED", "created_ms": at, "updated_ms": at, "last_touch_ref": f["book_ref"]}
                    self.journal(lane, seq, at, "CREATED", order.copy(), "feasible_target")
                    self.transition(lane, seq, at, order, "OPEN", "virtual_acceptance")
                for target in decision["targets"]:
                    self.anchor(lane, digest([decision_id, target["side"]]), "QUOTE", at, f["asset_id"], target["side"], target["price"], {"decision_id": decision_id, "input_seq": seq})
        self.markouts(event)

    def run(self, max_frames=None, close=False):
        run = self.db.execute("SELECT * FROM qe_runs WHERE id=?", (self.run_id,)).fetchone()
        if run["state"] == "CLOSED":
            return self.run_id
        query = "SELECT * FROM qe_input WHERE tape_id=? AND seq>? ORDER BY seq"
        params = [self.tape_id, run["cursor"]]
        if max_frames is not None:
            if max_frames < 0:
                raise ValueError("max_frames must be nonnegative")
            query += " LIMIT ?"
            params.append(max_frames)
        for row in self.db.execute(query, params).fetchall():
            with self.db, localcontext() as ctx:
                ctx.prec = PRECISION
                self.db.execute("BEGIN IMMEDIATE")
                current = self.db.execute("SELECT cursor,state FROM qe_runs WHERE id=?", (self.run_id,)).fetchone()
                if current["state"] == "CLOSED" or row["seq"] <= current["cursor"]:
                    continue
                if row["seq"] != current["cursor"]+1:
                    raise ValueError("cursor continuity failure")
                self.step(row)
                self.db.execute("UPDATE qe_runs SET cursor=?,state='PAUSED' WHERE id=?", (row["seq"], self.run_id))
        if close:
            cursor = self.db.execute("SELECT cursor FROM qe_runs WHERE id=?", (self.run_id,)).fetchone()[0]
            if cursor != self.manifest["count"]:
                raise ValueError("consume the complete tape before close")
            with self.db:
                for lane, _, _ in self.lanes:
                    for order in self.orders(lane):
                        if order["state"] in OPEN:
                            self.cancel(lane, cursor, self.manifest["end_ms"], order, "RUN_END")
                for mark in self.db.execute("SELECT * FROM qe_marks WHERE run_id=? AND status='PENDING'", (self.run_id,)).fetchall():
                    payload = json.loads(mark["payload"])
                    payload.update(reason="recording_ended_before_outcome_window", evaluated_ms=self.manifest["end_ms"])
                    self.db.execute("UPDATE qe_marks SET status='RIGHT_CENSORED',payload=? WHERE anchor_id=? AND kind=? AND horizon_s=?", (encode(payload), mark["anchor_id"], mark["kind"], mark["horizon_s"]))
                self.db.execute("UPDATE qe_runs SET state='CLOSED' WHERE id=?", (self.run_id,))
        self.audit()
        return self.run_id

    def audit(self):
        accounts, orders = {}, {}
        for row in self.db.execute("SELECT lane,payload FROM qe_journal WHERE run_id=? ORDER BY id", (self.run_id,)):
            p = json.loads(row["payload"])
            accounts[row["lane"]] = p["account"]
            if p.get("order"):
                orders[p["order"]["id"]] = p["order"]
        for lane, _, _ in self.lanes:
            if accounts.get(lane) != self.account(lane):
                raise ValueError("account cache differs from journal")
            for order in self.orders(lane):
                if orders.get(order["id"]) != order:
                    raise ValueError("order cache differs from journal")
        return {"run_id": self.run_id, "orders": len(orders), "real_submission_calls": 0}
