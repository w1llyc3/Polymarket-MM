import json
from contextlib import nullcontext
from dataclasses import asdict
from ..fair_value.maths import decimal
from ..fair_value.store import digest
from ..fill_management.ledger import Ledger, balance, valuation
from ..fill_management.storage import initialize
from ..paper_execution.manager import OrderManager
from ..paper_execution.risk import ACTIVE, commitments
from ..storage import encode
from .policy import BookPolicy
from .phase import PhaseController, install
from .reference import book_risk


class UnifiedPaperManager(OrderManager):
    """Same transaction: Paper receipt -> OMS inventory -> FIFO ledger.

    No initial shares; explicit collateral-funded split is separate and journaled.
    """
    supports_book_driven = True
    def __init__(self, store, policy=BookPolicy(), start_ms=0):
        if not isinstance(policy, BookPolicy):
            raise ValueError("unified inplay manager requires BookPolicy")
        saved = store.db.execute("SELECT config FROM pe_control WHERE id=1").fetchone()
        if saved and json.loads(saved[0]) != asdict(policy):
            # Refuse legacy/incompatible writers before creating a new ledger
            # or capital receipt. Historical results remain read-only auditable.
            raise ValueError("policy/version immutable; use a fresh v2.1 paper database")
        install(store.db)
        initialize(store.db)
        store.db.executescript("""
        CREATE TABLE IF NOT EXISTS ip_receipts(id TEXT PRIMARY KEY,order_id TEXT,filled TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS ip_income(id TEXT PRIMARY KEY,time_ms INTEGER,kind TEXT,stage TEXT,amount TEXT,market_id TEXT,evidence TEXT);
        CREATE TABLE IF NOT EXISTS ip_inputs(id TEXT PRIMARY KEY,time_ms INTEGER,payload TEXT);
        CREATE TRIGGER IF NOT EXISTS ip_inputs_u BEFORE UPDATE ON ip_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
        CREATE TRIGGER IF NOT EXISTS ip_inputs_d BEFORE DELETE ON ip_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
        CREATE TABLE IF NOT EXISTS ip_models(order_id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS ip_prints(id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS ip_fill_evidence(fill_id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS ip_flow(asset_id TEXT PRIMARY KEY,side TEXT,quantity TEXT,first_ms INTEGER);
        CREATE TABLE IF NOT EXISTS ip_session_config(id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS ip_flow_diagnostics(id INTEGER PRIMARY KEY,time_ms INTEGER,asset_id TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS ip_own_fills(id TEXT PRIMARY KEY,exchange_ms INTEGER,receipt_ms INTEGER,asset_id TEXT,side TEXT,quantity TEXT,payload TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS ip_unique_cash_receipt ON ip_income(json_extract(evidence,'$.cash_flow_id')) WHERE stage='RECEIVED';
        """)
        self.run_id = digest(["book-driven-unified-v2", asdict(policy)])
        self.ledger = Ledger(store.db, self.run_id, "UNIFIED_PAPER")
        if not self.ledger.state()["capital"] or self.ledger.state()["last_ms"] == -1:
            self.ledger.add(digest([self.run_id, "CAPITAL"]), start_ms, "CAPITAL", {"amount": policy.initial_cash, "source": "explicit virtual cash contribution"})
        self.phases = PhaseController(store.db, policy)
        super().__init__(store, policy, start_ms)
        self.ledger.audit()
        if not self.consistent():
            self.kill("unified_ledger_mismatch")

    def seed_asset(self, asset, event_id):
        # Parent uses initial_inventory=0, never manufactures holdings.
        super().seed_asset(asset, event_id)

    def register_market(self, market):
        outcomes = market["outcomes"]
        if len(outcomes) != 2 or len({o["asset_id"] for o in outcomes}) != 2:
            raise ValueError("binary market mapping required")
        native = {o["label"].upper() for o in outcomes} == {"YES", "NO"}
        with (nullcontext() if self.db.in_transaction else self.db):
            for index, outcome in enumerate(outcomes):
                asset = outcome["asset_id"]
                self.seed_asset(asset, market["event_id"])
                meta = {"asset_id": asset, "market_id": market["market_id"], "role": outcome["label"].upper() if native else "YES" if index == 0 else "NO", "label": outcome["label"], "complement": outcomes[1-index]["asset_id"]}
                key = digest([self.run_id, "ASSET", asset])
                old = self.db.execute("SELECT payload FROM l6_events WHERE id=?", (key,)).fetchone()
                if old:
                    if json.loads(old[0]) != meta:
                        raise ValueError("immutable asset mapping changed")
                else:
                    self.ledger.add(key, self.now, "ASSET", meta)

    def split(self, market, quantity, evidence_id):
        q = decimal(quantity)
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            if self.halted or q <= 0 or self.orders():
                raise ValueError("explicit split allowed only before trading in ACTIVE paper account")
            self.register_market(market)
            previous = self.db.execute("SELECT amount,market_id FROM ip_income WHERE id=?", (evidence_id,)).fetchone()
            if previous:
                if decimal(previous["amount"]) != -q or previous["market_id"] != market["market_id"]:
                    raise ValueError("split evidence ID collision")
                return False
            account = self.account()
            budget = commitments(account, self.orders())
            if q > decimal(account["cash"]) or budget["account_units"]+2*q > decimal(self.policy.max_account_risk) or budget["event_units"].get(market["event_id"], decimal(0))+2*q > decimal(self.policy.max_event_risk) or any(decimal(account["positions"].get(o["asset_id"], "0"))+q > decimal(self.policy.max_asset_inventory) for o in market["outcomes"]):
                raise ValueError("split cash or risk budget exceeded")
            for outcome in market["outcomes"]:
                asset = outcome["asset_id"]
                meta = self.ledger.state()["assets"][asset]
                self.ledger.add(digest([evidence_id, asset]), self.now, "FILL", {"asset_id": asset, **meta, "side": "BUY", "price": ".5", "quantity": str(q), "purpose": "SIMULATED_COMPLETE_SET_SPLIT", "fee": "0", "context": {"basis": "equal split of $1 collateral, not probability"}})
                account["positions"][asset] = str(decimal(account["positions"][asset])+q)
                account["seed_positions"][asset] = str(decimal(account["seed_positions"][asset])+q)
            account["cash"] = str(decimal(account["cash"])-q)
            account["seed_cash"] = str(decimal(account["seed_cash"])-q)
            self._account(account)
            self.db.execute("INSERT INTO ip_income VALUES(?,?,'SPLIT','CASH',?,?,?)", (evidence_id, self.now, str(-q), market["market_id"], encode({"synthetic": True, "collateral": str(q)})))
            self._log("COLLATERAL_FUNDED_PAPER_SPLIT", detail={"quantity_each": str(q), "cash_debit": str(q), "evidence_id": evidence_id})
            return True

    def enqueue(self, request_id, target, frame, min_edge="0.01"):
        if any(k in target or k in frame for k in ("reversal", "reversal_signal", "predicted_winner", "final_result", "win_probability", "fair_probability")):
            raise ValueError("directional/reversal/prediction fields forbidden in BOOK_DRIVEN input")
        current = self.prepare_submission_frame(None, frame)
        return super().enqueue(request_id, target, current, min_edge)

    def prepare_submission_frame(self, order, frame):
        frame = self.phase_frame(frame)
        return {**frame, "phase": self.phases.boundary(frame, self.now)}

    def phase_frame(self, frame):
        if not self.policy.allow_manual_phase:
            return {k: v for k, v in frame.items() if k != "manual_confirmation"}
        latest = None
        for row in self.db.execute("SELECT payload FROM ip_manual_confirmations WHERE available_ms<=? ORDER BY available_ms DESC,rowid DESC", (self.now,)):
            candidate = json.loads(row[0])
            if candidate.get("market_id") == frame["market_id"]:
                latest = candidate
                break
        return {**frame, "manual_confirmation": latest, "phase_authority": "MANUAL_CONFIRMED", "not_unattended": True}

    def _report(self, order_id, payload, reconciled=False):
        previous = decimal(self.order(order_id)["filled"])
        super()._report(order_id, payload, reconciled)
        order = self.order(order_id)
        cumulative = decimal(order["filled"])
        if cumulative <= previous:
            return
        qty = cumulative-previous
        asset = order["asset_id"]
        meta = self.ledger.state()["assets"][asset]
        frame = order["frame"]
        quote_mid = str((decimal(frame["book"]["bid"])+decimal(frame["book"]["ask"]))/2)
        fill_key = payload.get("report_id", "").removeprefix("fill:")
        fill_evidence = self.db.execute("SELECT payload FROM ip_fill_evidence WHERE fill_id=?", (fill_key,)).fetchone()
        mid = json.loads(fill_evidence[0]).get("reference_mid_at_fill") if fill_evidence else None
        event_id = digest([self.run_id, order_id, str(cumulative)])
        context = {"strategy_mode": "BOOK_DRIVEN", "reference_mid": mid, "quote_mid": quote_mid, "book_ref": frame.get("book_ref"),
                   "confidence": "uncalibrated_paper_proxy", "received_ms": self.now,
                   "exchange_ms": payload.get("exchange_ms"), "stale_quote": self.now-frame["book_received_ms"] > self.policy.book_max_age_ms}
        self.ledger.add(event_id, self.now, "FILL", {"asset_id": asset, **meta, "order_id": order_id, "side": order["side"], "price": order["price"], "quantity": str(qty), "fee": "0", "purpose": order.get("purpose", "MAKER"), "context": context})
        self.db.execute("INSERT INTO ip_receipts VALUES(?,?,?,?)", (event_id, order_id, str(cumulative), encode(context)))
        position = decimal(self.account()["positions"][asset])
        other = decimal(self.account()["positions"].get(meta["complement"], "0"))
        before = position-qty if order["side"] == "BUY" else position+qty
        self.db.execute("INSERT INTO ip_own_fills VALUES(?,?,?,?,?,?,?)", (event_id, payload.get("exchange_ms"), self.now, asset, order["side"], str(qty), encode({"net_directional_before": str(before-other), "net_directional_after": str(position-other), "absolute_directional_increase": str(abs(position-other)-abs(before-other)), "public_flow_is_not_own_fill": True})))
        self._log("UNIFIED_FIFO_RECEIPT", order, {"ledger_event_id": event_id, "quantity": str(qty)})

    def consistent(self):
        if not super().consistent():
            return False
        if not hasattr(self, "ledger"):
            return True
        state, account = self.ledger.state(), self.account()
        return self.ledger.audit() and decimal(state["cash"]) == decimal(account["cash"]) and all(balance(state, a) == decimal(q) for a, q in account["positions"].items())

    def book_risk_check(self, frame, previous_mid=None):
        at = self.now
        reasons = book_risk(frame, self.policy, at)
        if previous_mid is not None and "book" in frame:
            try:
                mid = (decimal(frame["book"]["bid"])+decimal(frame["book"]["ask"]))/2
                if abs(mid-decimal(previous_mid)) > decimal(self.policy.max_price_jump):
                    if self.policy.enhanced:
                        reasons.append("abrupt_price_change")
                    else:
                        self._risk("observed_abrupt_price_change_baseline", evidence={"frame": frame, "monitor_only": True})
            except (ValueError, KeyError, TypeError, ArithmeticError):
                reasons.append("invalid_prices")
        if any(o["state"] in {"UNKNOWN", "TIMEOUT", "RECONCILED"} for o in self.orders()):
            reasons.append("unknown_order_state")
        with self.db:
            self.db.execute("INSERT INTO pe_risk_frames VALUES(?,?,?) ON CONFLICT(asset_id) DO UPDATE SET time_ms=excluded.time_ms,payload=excluded.payload WHERE excluded.time_ms>=pe_risk_frames.time_ms", (frame["asset_id"], frame["time_ms"], encode(frame)))
            self.metric("risk_detection", 0)
            # Phase is a permission gate, not a requirement to permanently kill
            # an otherwise clean pre-game observer. Live data faults with active
            # risk are severe and require reconciliation/manual reset.
            severe = [r for r in reasons if r != "phase_not_permitted"]
            for reason in reasons:
                self._risk(reason, evidence={"frame": frame})
            if not self.policy.enhanced:
                for side in ("bids", "asks"):
                    levels = (frame.get("book") or {}).get(side, [])
                    if levels and decimal(levels[0]["size"]) < decimal(self.policy.min_top_depth):
                        self._risk("observed_depth_drained_baseline", evidence={"asset_id": frame["asset_id"], "side": side, "monitor_only": True})
            if severe and (any(o["state"] in ACTIVE for o in self.orders()) or any(decimal(q) > 0 for q in self.account()["positions"].values())):
                self._kill(";".join(sorted(set(severe))))
        return sorted(set(reasons))

    def mark_equity(self, frames):
        """Valuation quality is independent of quote phase permission."""
        marks = {}
        permission_only = {"phase_not_permitted", "late_close_threshold", "late_reduce_boundary", "unreliable_sports_state", "sports_stale_or_future", "invalid_clock"}
        for f in frames:
            if not set(book_risk(f, self.policy, self.now))-permission_only:
                marks[f["asset_id"]] = (decimal(f["book"]["bid"])+decimal(f["book"]["ask"]))/2
        with self.db:
            account = self.account()
            if any(decimal(q) and asset not in marks for asset, q in account["positions"].items()):
                self._kill("daily_equity_unavailable")
                return None
            equity = decimal(account["cash"])+sum((decimal(q)*marks.get(a, decimal(0)) for a, q in account["positions"].items()), decimal(0))
            daily = account["daily"].setdefault(str(self.now//86400000), {"baseline": str(equity), "baseline_ms": self.now})
            pnl = equity-decimal(daily["baseline"])
            daily.update(equity=str(equity), paper_pnl=str(pnl), observed_ms=self.now)
            self._account(account)
            self._log("BOOK_PAPER_EQUITY", detail={"marks": {a: str(v) for a, v in marks.items()}, "paper_pnl": str(pnl)})
            if pnl <= -decimal(self.policy.max_daily_loss):
                self._kill("daily_paper_loss_circuit_breaker")
            return str(pnl)

    def income(self, event_id, kind, stage, amount, evidence, market_id=None):
        if kind not in {"MAKER_REBATE", "REWARD", "RUNNING_COST", "SIMULATED_COST"} or stage not in {"EXPECTED", "CONFIRMED", "RECEIVED"}:
            raise ValueError("explicit income kind and stage required")
        amount = decimal(amount)
        if amount < 0 or not evidence or not 0 <= evidence.get("observed_ms", self.now+1) <= self.now:
            raise ValueError("as-of income evidence required")
        if stage == "RECEIVED" and (not evidence.get("cash_flow_id") or not evidence.get("provenance")):
            raise ValueError("cash receipt requires independent cash flow provenance")
        if market_id is not None and market_id not in {m["market_id"] for m in self.ledger.state()["assets"].values()}:
            raise ValueError("cannot allocate income to unknown market")
        with self.db:
            old = self.db.execute("SELECT * FROM ip_income WHERE id=?", (event_id,)).fetchone()
            values = (event_id, self.now, kind, stage, str(amount), market_id, encode(evidence))
            if old:
                if tuple(old) != values:
                    raise ValueError("income identity collision")
                return False
            if stage == "RECEIVED":
                if kind in {"RUNNING_COST", "SIMULATED_COST"}:
                    self.ledger.add(event_id, self.now, "COST", {"amount": str(amount), "evidence": evidence})
                    sign = -1
                else:
                    self.ledger.add(event_id, self.now, "REWARD", {"amount": str(amount), "evidence": evidence})
                    sign = 1
                account = self.account()
                account["cash"] = str(decimal(account["cash"])+sign*amount)
                # Independent authority starts from explicit paper transfers.
                account["seed_cash"] = str(decimal(account["seed_cash"])+sign*amount)
                self._account(account)
            self.db.execute("INSERT INTO ip_income VALUES(?,?,?,?,?,?,?)", values)
            self._log("PAPER_INCOME_STAGE", detail={"kind": kind, "stage": stage, "amount": str(amount), "market_id": market_id, "unallocated": market_id is None})
        return True
