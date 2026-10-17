"""Durable Order Manager. A PAPER adapter is the only supported authority."""
import json
from dataclasses import asdict
from decimal import localcontext

from ..storage import encode
from ..fair_value.maths import PRECISION, decimal
from ..fair_value.store import digest
from ..fair_value.providers import reject_secrets
from ..quote_engine.model import hard_risk
from .adapter import PaperExchangeAdapter
from .risk import ACTIVE, TERMINAL, ExecutionPolicy, commitments, order_risk

STATES = {"CREATED", "QUEUED", "SUBMITTING", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELLED", "REJECTED", "TIMEOUT", "UNKNOWN", "RECONCILED"}
TRANSITIONS = {
    "CREATED": {"QUEUED", "REJECTED", "CANCELLED"}, "QUEUED": {"SUBMITTING", "REJECTED", "CANCELLED"},
    "SUBMITTING": {"OPEN", "PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCEL_PENDING", "TIMEOUT", "UNKNOWN"},
    "OPEN": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "TIMEOUT", "UNKNOWN", "RECONCILED"},
    "PARTIALLY_FILLED": {"FILLED", "CANCEL_PENDING", "TIMEOUT", "UNKNOWN", "RECONCILED"},
    "CANCEL_PENDING": {"FILLED", "CANCELLED", "REJECTED", "TIMEOUT", "UNKNOWN", "RECONCILED"},
    "TIMEOUT": {"UNKNOWN"}, "UNKNOWN": {"RECONCILED"}, "RECONCILED": {"UNKNOWN", "FILLED", "CANCELLED", "REJECTED"},
    **{s: {"UNKNOWN"} for s in TERMINAL}}


class OrderManager:
    real_submission_calls = 0

    def __init__(self, store, policy=ExecutionPolicy(), start_ms=0):
        if policy.strategy_mode == "BOOK_DRIVEN" and not getattr(self, "supports_book_driven", False):
            raise ValueError("BOOK_DRIVEN requires unified manager and persisted phase authority")
        self.store, self.db, self.policy = store, store.db, policy
        self.adapter = PaperExchangeAdapter(self.db)
        with self.db:
            row = self.db.execute("SELECT * FROM pe_control WHERE id=1").fetchone()
            if not row:
                account = {"cash": policy.initial_cash, "positions": {}, "events": {}, "seed_positions": {}, "seed_cash": policy.initial_cash, "daily": {}}
                self.db.execute("INSERT INTO pe_control VALUES(1,'PAPER','ACTIVE',?,?,?)", (start_ms, encode(asdict(policy)), encode(account)))
                self._log("INITIALIZED", None, {"mode": "PAPER"})
            else:
                stored_policy = json.loads(row["config"])
                stored_policy.setdefault("strategy_mode", "EXTERNAL_FAIR")
                if stored_policy != asdict(policy):
                    raise ValueError("policy is immutable across restarts")
                self._restore()
                for order in self.orders():
                    if order["state"] in ACTIVE and order["state"] != "QUEUED":
                        order.update(state="UNKNOWN", unknown_since_ms=self.now)
                        self._save(order, "SERVICE_RESTART_UNKNOWN")
                self._kill("service_restart_requires_authoritative_reconciliation")

    @property
    def now(self):
        return self.db.execute("SELECT clock_ms FROM pe_control WHERE id=1").fetchone()[0]

    @property
    def halted(self):
        return self.risk_state != "ACTIVE"

    @property
    def risk_state(self):
        state = self.db.execute("SELECT state FROM pe_control WHERE id=1").fetchone()[0]
        return "ACTIVE" if state == "RUNNING" else state

    def pause(self, reason="operator_pause"):
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            if self.risk_state != "ACTIVE":
                return False
            self._kill(reason)
            self.db.execute("UPDATE pe_control SET state='PAUSED' WHERE id=1")
            self._log("PAUSED", detail={"reason": reason})
            return True

    def account(self):
        return json.loads(self.db.execute("SELECT account FROM pe_control WHERE id=1").fetchone()[0])

    def orders(self,active_only=False):
        if active_only:
            states=sorted(ACTIVE)
            return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM pe_orders WHERE state IN ('+','.join('?' for _ in states)+') ORDER BY rowid',states)]
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM pe_orders ORDER BY rowid")]

    def order(self, order_id):
        row = self.db.execute("SELECT payload FROM pe_orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            raise ValueError("unknown local intent")
        return json.loads(row[0])

    def _log(self, kind, order=None, detail=None):
        control = dict(self.db.execute("SELECT * FROM pe_control WHERE id=1").fetchone())
        self.db.execute("INSERT INTO pe_journal(time_ms,kind,order_id,payload) VALUES(?,?,?,?)", (self.now, kind, order["id"] if order else None, encode({"order": order, "control": control, "detail": detail, "virtual": True})))

    def _save(self, order, kind, detail=None):
        if order["state"] not in STATES:
            raise ValueError("invalid order state")
        previous = self.db.execute("SELECT state FROM pe_orders WHERE id=?", (order["id"],)).fetchone()
        if previous and order["state"] != previous[0] and order["state"] not in TRANSITIONS[previous[0]]:
            raise ValueError(f"invalid paper transition {previous[0]} -> {order['state']}")
        order["updated_ms"] = self.now
        self.db.execute("INSERT INTO pe_orders VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,payload=excluded.payload", (order["id"], order["state"], encode(order)))
        self._log(kind, order.copy(), detail)

    def _account(self, account):
        self.db.execute("UPDATE pe_control SET account=? WHERE id=1", (encode(account),))

    def metric(self, kind, duration, order_id=None):
        if duration < 0:
            raise ValueError("negative virtual duration")
        self.db.execute("INSERT INTO pe_latency(time_ms,kind,duration_ms,order_id,provenance) VALUES(?,?,?,?,'synthetic_paper_clock')", (self.now, kind, duration, order_id))

    def _risk(self, reason, order=None, evidence=None):
        self.db.execute("INSERT INTO pe_risks(time_ms,order_id,reason,payload) VALUES(?,?,?,?)", (self.now, order["id"] if order else None, reason, encode(evidence)))
        self._log("RISK", order, {"reason": reason, "evidence": evidence})

    def seed_asset(self, asset, event_id):
        account = self.account()
        if asset not in account["positions"]:
            account["positions"][asset] = self.policy.initial_inventory
            account["seed_positions"][asset] = self.policy.initial_inventory
            account["events"][asset] = event_id
            self._account(account)
            self._log("VIRTUAL_ASSET_SEED", detail={"asset_id": asset, "event_id": event_id})
        elif account["events"][asset] != event_id:
            raise ValueError("asset/event identity changed")

    def enqueue(self, request_id, target, frame, min_edge="0.01"):
        reject_secrets(target)
        reject_secrets(frame)
        order_id = digest(["paper-intent", request_id])
        with self.db, localcontext() as ctx:
            ctx.prec = PRECISION
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute("SELECT payload FROM pe_orders WHERE id=?", (order_id,)).fetchone()
            if existing:
                old = json.loads(existing[0])
                if old["target"] != target or old["asset_id"] != frame["asset_id"]:
                    raise ValueError("client intent reused with different target")
                return order_id
            order = {**target, "id": order_id, "request_id": request_id, "asset_id": frame["asset_id"], "market_id": frame["market_id"], "event_id": frame["market"]["event_id"], "filled": "0", "state": "CREATED", "created_ms": self.now, "min_edge": min_edge, "frame": frame, "target": target, "virtual": True}
            if not self.halted:
                self.seed_asset(order["asset_id"], order["event_id"])
            reasons = ["kill_switch_halted"] if self.halted else order_risk(order, frame, self.account(), self.orders(active_only=True), self.policy, self.now)
            self._save(order, "CREATED")
            if reasons:
                order["state"] = "REJECTED"
                self._save(order, "LOCAL_REJECTION", reasons)
                for reason in reasons:
                    self._risk(reason, order, {"frame": frame})
            else:
                order["state"] = "QUEUED"
                self._save(order, "QUEUED")
        return order_id

    def dispatch(self, response_ms=None, drop_response=False, accepted=True):
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            if self.halted:
                return 0
            count = 0
            for order in self.orders(active_only=True):
                if order["state"] != "QUEUED":
                    continue
                others = [o for o in self.orders(active_only=True) if o["id"] != order["id"]]
                latest = self.db.execute("SELECT payload FROM pe_risk_frames WHERE asset_id=?", (order["asset_id"],)).fetchone()
                current = json.loads(latest[0]) if latest else order["frame"]
                if current["time_ms"] < order["frame"]["time_ms"]:
                    current = order["frame"]
                current = self.prepare_submission_frame(order, current)
                reasons = order_risk(order, current, self.account(), others, self.policy, self.now)
                if reasons:
                    order["state"] = "REJECTED"
                    self._save(order, "DISPATCH_RISK_REJECTED", reasons)
                    for reason in reasons:
                        self._risk(reason, order)
                    continue
                order.update(state="SUBMITTING", submit_ms=self.now, submit_deadline_ms=self.now+self.policy.request_timeout_ms)
                self._save(order, "SUBMIT_REQUEST")
                self.adapter.submit(order, self.now, self.policy.submit_ack_ms if response_ms is None else response_ms, accepted, drop_response)
                count += 1
            return count

    def prepare_submission_frame(self, order, frame):
        return frame

    def _cancel(self, order, drop_response=False):
        if order["state"] == "RECONCILED" and order.get("confirmed_state") in TERMINAL:
            self._log("CANCEL_AUTHORITY_ALREADY_TERMINAL", order)
            return
        if order["state"] in TERMINAL:
            self._log("CANCEL_TOO_LATE_OR_TERMINAL", order)
            return
        if order["state"] in {"CREATED", "QUEUED"}:
            order["state"] = "CANCELLED"
            self._save(order, "LOCAL_QUEUE_CLEARED")
            return
        if order.get("cancel_deadline_ms") is not None:
            return
        if order["state"] not in {"UNKNOWN", "RECONCILED"}:
            order["state"] = "CANCEL_PENDING"
        order.update(cancel_requested_ms=self.now, cancel_deadline_ms=self.now+self.policy.request_timeout_ms)
        self._save(order, "CANCEL_REQUEST", {'reason':order.get('cancel_reason','EXPIRY' if order.get('expiry_handled') else 'LEGACY_UNCLASSIFIED')})
        self.adapter.cancel(order["id"], self.now, self.policy.cancel_latency_ms, drop_response)

    def cancel(self, order_id, drop_response=False, reason=None):
        with self.db:
            order=self.order(order_id)
            if reason:order['cancel_reason']=reason
            self._cancel(order, drop_response)

    def _kill(self, reason):
        self.db.execute("UPDATE pe_control SET state='HALTED' WHERE id=1")
        self._risk(reason, evidence={"kill_switch": True})
        for order in self.orders(active_only=True):
            if order["state"] in ACTIVE:
                order['cancel_reason']='OPERATOR' if any(x in reason for x in ('operator','prepare_direction')) else 'ACCOUNT_RISK'
                self._risk(reason, order, {"kill_switch": True})
                self._cancel(order)
        self._log("KILL_SWITCH", detail={"reason": reason, "new_risk_allowed": False})

    def kill(self, reason):
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            self._kill(reason)

    def _report(self, order_id, payload, reconciled=False):
        report_id = payload["report_id"]
        if not self.db.execute("INSERT OR IGNORE INTO pe_seen_reports VALUES(?)", (report_id,)).rowcount:
            self._log("DUPLICATE_REPORT", detail={"report_id": report_id})
            return
        order, authority = self.order(order_id), payload["authority"]
        if not authority:
            order.update(state="UNKNOWN", unknown_since_ms=order.get("unknown_since_ms", self.now))
            self._save(order, "AUTHORITY_MISSING")
            self._kill("authority_missing")
            return
        if any(authority[k] != order[k] for k in ("id", "asset_id", "side", "price", "size")):
            self._kill("authority_identity_mismatch")
            return
        cumulative = decimal(authority["filled"])
        previous = decimal(order["filled"])
        if cumulative < previous:
            self._log("OUT_OF_ORDER_REPORT", order, payload)
            return
        if cumulative > decimal(order["size"]):
            self._kill("authority_overfill")
            return
        if order["state"] in TERMINAL and cumulative > previous:
            order.update(state="UNKNOWN", unknown_since_ms=self.now)
            self._save(order, "LATE_TERMINAL_FILL_REQUIRES_RECONCILIATION")
            self._kill("late_terminal_fill_correction")
        delta = cumulative-previous
        if delta:
            account = self.account()
            asset, price = order["asset_id"], decimal(order["price"])
            buying = order["side"] == "BUY"
            account["cash"] = str(decimal(account["cash"])+price*delta*(-1 if buying else 1))
            account["positions"][asset] = str(decimal(account["positions"][asset])+(delta if buying else -delta))
            self._account(account)
            order["filled"] = str(cumulative)
        old_state = order["state"]
        order["confirmed_state"] = authority["state"]
        if old_state in {"UNKNOWN", "TIMEOUT", "RECONCILED"} or reconciled:
            order["state"] = "RECONCILED"
        elif old_state == "CANCEL_PENDING" and authority["state"] in {"OPEN", "PARTIALLY_FILLED"}:
            order["state"] = "CANCEL_PENDING"
        elif old_state in TERMINAL and cumulative == previous:
            self._log("LATE_REPORT_IGNORED", order, payload)
            return
        else:
            order["state"] = authority["state"]
        if authority["state"] in TERMINAL or reconciled:
            order["cancel_deadline_ms"] = None
        order["submit_deadline_ms"] = None
        self.metric("data_transport", max(0, self.now-payload["exchange_ms"]), order_id)
        if "cancel_requested_ms" in payload:
            self.metric("simulated_cancel", self.now-payload["cancel_requested_ms"], order_id)
        if reconciled:
            self.metric("state_recovery", self.now-order.get("unknown_since_ms", self.now), order_id)
        self._save(order, "RECONCILED" if reconciled else "EXECUTION_REPORT", {**payload, "delta_fill": str(delta)})
        account = self.account()
        if decimal(account["cash"]) < 0 or any(decimal(q) < 0 for q in account["positions"].values()):
            self._kill("ledger_negative_balance")

    def advance(self, to_ms):
        if type(to_ms) is not int or to_ms < self.now:
            raise ValueError("paper clock must move forward")
        with self.db, localcontext() as ctx:
            ctx.prec = PRECISION
            while True:
                pending = self.db.execute("SELECT * FROM pe_pending WHERE done=0 AND due_ms<=? ORDER BY due_ms,id LIMIT 1", (to_ms,)).fetchone()
                deadlines = [o[k] for o in self.orders(active_only=True) for k in ("submit_deadline_ms", "cancel_deadline_ms") if o.get(k) is not None and o[k] <= to_ms]
                deadlines += [o["expires_ms"] for o in self.orders(active_only=True) if o["state"] in ACTIVE and not o.get("expiry_handled") and o["expires_ms"] <= to_ms]
                deadline = min(deadlines) if deadlines else None
                if not pending and deadline is None:
                    break
                # Exchange reports at the exact deadline win; a missing report never means cancellation succeeded.
                if pending and (deadline is None or pending["due_ms"] <= deadline):
                    self.db.execute("UPDATE pe_control SET clock_ms=? WHERE id=1", (max(self.now, pending["due_ms"]),))
                    payload = json.loads(pending["payload"])
                    self.db.execute("UPDATE pe_pending SET done=1 WHERE id=?", (pending["id"],))
                    if pending["kind"] == "EXCHANGE_CANCEL":
                        self.adapter.apply_cancel(pending["order_id"], self.now, payload)
                    else:
                        self._report(pending["order_id"], payload)
                else:
                    self.db.execute("UPDATE pe_control SET clock_ms=? WHERE id=1", (max(self.now, deadline),))
                    for order in self.orders(active_only=True):
                        if order["state"] in ACTIVE and not order.get("expiry_handled") and order["expires_ms"] <= self.now:
                            order["expiry_handled"] = True
                            self._risk("intent_expired", order)
                            self._save(order, "EXPIRY_DETECTED")
                            self._cancel(order)
                        expired = [k for k in ("submit_deadline_ms", "cancel_deadline_ms") if order.get(k) is not None and order[k] <= self.now]
                        if expired:
                            for key in expired:
                                order[key] = None
                            order.update(state="TIMEOUT", unknown_since_ms=self.now)
                            self._save(order, "REQUEST_TIMEOUT", expired)
                            order["state"] = "UNKNOWN"
                            self._save(order, "UNKNOWN", "timeout_is_not_success")
                            self._kill("request_timeout")
            self.db.execute("UPDATE pe_control SET clock_ms=? WHERE id=1", (to_ms,))
            self._log("CLOCK_ADVANCE")

    def fill(self, order_id, size, event_id, transport_ms=0):
        with self.db:
            return self.adapter.fill(order_id, size, self.now, event_id, transport_ms)

    def reconcile(self, latency_ms=None):
        delay = self.policy.rest_latency_ms if latency_ms is None else latency_ms
        if type(delay) is not int or delay < 0:
            raise ValueError("invalid paper REST latency")
        start = self.now
        with self.db:
            self.db.execute("UPDATE pe_control SET state='RECONCILING' WHERE id=1")
            self._log("RECONCILIATION_BARRIER")
        self.advance(start+min(delay, self.policy.rest_timeout_ms))
        with self.db:
            self.metric("authority_query", self.now-start)
            if delay > self.policy.rest_timeout_ms:
                self._kill("authoritative_query_timeout")
                return False
            for order in self.orders():
                if order["state"] in ACTIVE:
                    self._report(order["id"], {"authority": self.adapter.authoritative(order["id"]), "report_id": f"reconcile:{order['id']}:{self.now}", "exchange_ms": self.now}, True)
            consistent = self.consistent()
            if not consistent:
                self._kill("authority_ledger_mismatch")
            elif not any(o["state"] in {"UNKNOWN", "TIMEOUT", "SUBMITTING", "CANCEL_PENDING"} or o.get("cancel_deadline_ms") is not None or (o["state"] == "RECONCILED" and o.get("confirmed_state") not in TERMINAL) for o in self.orders()):
                self.db.execute("UPDATE pe_control SET state='READY_FOR_MANUAL_RESET' WHERE id=1")
            else:
                self.db.execute("UPDATE pe_control SET state='HALTED' WHERE id=1")
            self._log("RECONCILIATION_COMPLETE", detail={"consistent": consistent, "halted": self.halted})
            return consistent

    def consistent(self):
        account = self.account()
        local_ids = {o["id"] for o in self.orders()}
        if any(r[0] not in local_ids for r in self.db.execute("SELECT id FROM pe_exchange")):
            return False
        cash = decimal(account["seed_cash"])
        positions = {a: decimal(q) for a, q in account["seed_positions"].items()}
        for order in self.orders():
            authority = self.adapter.authoritative(order["id"])
            if not authority:
                if order["state"] not in {"QUEUED", "CANCELLED", "REJECTED"}:
                    return False
                continue
            if any(authority.get(k) != order.get(k) for k in ("id", "asset_id", "market_id", "event_id", "side", "price", "size")):
                return False
            filled = decimal(authority["filled"])
            if filled != decimal(order["filled"]):
                return False
            buying = order["side"] == "BUY"
            cash += decimal(order["price"])*filled*(-1 if buying else 1)
            positions[order["asset_id"]] += filled if buying else -filled
            expected = order.get("confirmed_state", order["state"])
            if order["state"] == "UNKNOWN" or (expected not in {authority["state"], "CANCEL_PENDING"} and order["state"] != "SUBMITTING"):
                return False
        authority_account = self.adapter.balance_snapshot(account["seed_cash"], account["seed_positions"])
        return (cash == decimal(account["cash"]) and positions == {a: decimal(q) for a, q in account["positions"].items()}
                and authority_account == {"cash": cash, "positions": positions})

    def resume(self):
        """Explicit operator action; never called automatically after UNKNOWN."""
        with self.db:
            if self.risk_state not in {"ACTIVE", "READY_FOR_MANUAL_RESET"}:
                self._risk("manual_reset_requires_completed_reconciliation")
                self._log("RESET_BLOCKED")
                return False
            daily = self.account()["daily"].get(str(self.now//86400000), {})
            if daily.get("paper_pnl") is not None and decimal(daily["paper_pnl"]) <= -decimal(self.policy.max_daily_loss):
                self._kill("resume_blocked_by_daily_paper_loss")
                return False
            if not self.consistent() or any(o["state"] in {"UNKNOWN", "TIMEOUT", "SUBMITTING", "CANCEL_PENDING"} or o.get("cancel_deadline_ms") is not None for o in self.orders()):
                self._kill("resume_blocked_by_uncertain_orders")
                return False
            if any(o["state"] == "RECONCILED" and o["confirmed_state"] not in TERMINAL for o in self.orders()):
                self._kill("resume_blocked_by_active_authority")
                return False
            for order in self.orders():
                if order["state"] == "RECONCILED":
                    if order["confirmed_state"] not in TERMINAL:
                        self._kill("resume_blocked_by_active_authority")
                        return False
                    order["state"] = order["confirmed_state"]
                    self._save(order, "OPERATOR_ACCEPTED_RECONCILIATION")
            self.db.execute("UPDATE pe_control SET state='ACTIVE' WHERE id=1")
            self._log("OPERATOR_RESUME")
            return True

    def _restore(self):
        latest, orders = None, {}
        for row in self.db.execute("SELECT payload FROM pe_journal ORDER BY seq"):
            p = json.loads(row[0])
            latest = p["control"]
            if p.get("order"):
                orders[p["order"]["id"]] = p["order"]
        if not latest:
            raise ValueError("missing durable intent journal")
        self.db.execute("UPDATE pe_control SET clock_ms=?,state=?,account=? WHERE id=1", (latest["clock_ms"], latest["state"], latest["account"]))
        for order in orders.values():
            self.db.execute("INSERT INTO pe_orders VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,payload=excluded.payload", (order["id"], order["state"], encode(order)))

    def risk_check(self, frame, previous_mid=None):
        if self.policy.strategy_mode == "BOOK_DRIVEN":
            return self.book_risk_check(frame, previous_mid)
        start = self.now
        self.advance(start+self.policy.risk_detection_ms)
        with self.db:
            self.db.execute("INSERT INTO pe_risk_frames VALUES(?,?,?) ON CONFLICT(asset_id) DO UPDATE SET time_ms=excluded.time_ms,payload=excluded.payload WHERE excluded.time_ms>=pe_risk_frames.time_ms", (frame["asset_id"], frame["time_ms"], encode(frame)))
            reasons = hard_risk(frame, self.policy)
            at = self.now
            try:
                config = frame["signal"]["config"]
                if at-frame["book_received_ms"] > config["book_max_age_ms"]:
                    reasons.append("book_stale_at_detection")
                if any(at-s[k] > config["odds_max_age_ms"] for s in frame["signal"]["sources"] if s.get("included") for k in ("updated_ms", "received_ms")):
                    reasons.append("odds_stale_at_detection")
                mid = (decimal(frame["book"]["bid"])+decimal(frame["book"]["ask"]))/2
                if previous_mid is not None and abs(mid-decimal(previous_mid)) > decimal(self.policy.max_price_jump):
                    reasons.append("abrupt_price_change")
                for order in self.orders():
                    if order["state"] in ACTIVE and order["asset_id"] == frame["asset_id"]:
                        fair, price = decimal(frame["signal"]["fair_probability"]), decimal(order["price"])
                        if (fair-price if order["side"] == "BUY" else price-fair) < decimal(order["min_edge"]):
                            reasons.append("edge_lost")
                if at >= frame["market"]["game_start_ms"]-self.policy.start_guard_ms:
                    reasons.append("start_protection")
            except (ValueError, TypeError, KeyError):
                reasons.append("invalid_risk_reference")
            if any(o["state"] in {"UNKNOWN", "TIMEOUT", "RECONCILED"} for o in self.orders()):
                reasons.append("unknown_order_state")
            budget = commitments(self.account(), self.orders())
            if budget["account_units"] > decimal(self.policy.max_account_risk):
                reasons.append("account_committed_risk")
            if any(q > decimal(self.policy.max_event_risk) for q in budget["event_units"].values()):
                reasons.append("event_committed_risk")
            self.metric("risk_detection", self.now-start)
            if reasons:
                for reason in sorted(set(reasons)):
                    self._risk(reason, evidence={"frame": frame})
                self._kill(";".join(sorted(set(reasons))))
            self._log("RISK_CHECK", detail={"frame": frame, "reasons": reasons})
            return sorted(set(reasons))

    def mark_equity(self, frames):
        """Paper mark-to-mid daily loss; requires fresh evidence for every held asset."""
        with self.db:
            account, marks = self.account(), {}
            for f in frames:
                age_limit = self.policy.book_max_age_ms if self.policy.strategy_mode == "BOOK_DRIVEN" else f.get("signal", {}).get("config", {}).get("book_max_age_ms", 0)
                if not hard_risk(f, self.policy) and 0 <= self.now-f["time_ms"] <= age_limit:
                    marks[f["asset_id"]] = (decimal(f["book"]["bid"])+decimal(f["book"]["ask"]))/2
            held = {a for a, q in account["positions"].items() if decimal(q) != 0}
            if not held <= marks.keys():
                self._kill("daily_equity_unavailable")
                return None
            equity = decimal(account["cash"])+sum((decimal(q)*marks.get(a, decimal(0)) for a, q in account["positions"].items()), decimal(0))
            day = str(self.now//86400000)
            daily = account["daily"].setdefault(day, {"baseline": str(equity), "baseline_ms": self.now})
            pnl = equity-decimal(daily["baseline"])
            daily.update(equity=str(equity), paper_pnl=str(pnl), observed_ms=self.now)
            self._account(account)
            self._log("PAPER_EQUITY", detail={"frames": frames, "paper_pnl": str(pnl), "not_real_profit": True})
            if pnl <= -decimal(self.policy.max_daily_loss):
                self._kill("daily_paper_loss_circuit_breaker")
            return str(pnl)

    @staticmethod
    def latency_summary(db):
        metrics = {}
        for kind, in db.execute("SELECT DISTINCT kind FROM pe_latency"):
            samples = [r[0] for r in db.execute("SELECT duration_ms FROM pe_latency WHERE kind=? ORDER BY duration_ms", (kind,))]
            from math import ceil
            metrics[kind] = {"count": len(samples), **{f"P{p}": samples[max(0, ceil(len(samples)*p/100)-1)] for p in (50, 95, 99)}}
        return metrics

    def status(self):
        metrics = self.latency_summary(self.db)
        budget = commitments(self.account(), self.orders())
        return {"mode": "PAPER", "risk_state": self.risk_state, "halted": self.halted, "clock_ms": self.now, "orders": self.orders(), "account": self.account(),
                "commitments": {k: {a: str(v) for a, v in value.items()} if isinstance(value, dict) else str(value) for k, value in budget.items()},
                "latency_ms": metrics, "latency_provenance": "synthetic_paper_clock", "real_submission_calls": 0}
