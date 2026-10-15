"""Three counterfactual models over the Lesson 4 sealed stream; no network execution."""
import json
from dataclasses import asdict
from decimal import localcontext

from ..fair_value.maths import PRECISION, decimal
from ..fair_value.store import digest
from ..quote_engine.executor import Executor
from ..quote_engine.model import OPEN, Policy, grid, hard_risk
from ..storage import encode
from .storage import initialize
from .ledger import Ledger, balance, valuation
from .models import ASSUMPTIONS, ModelConfig, order_quantities, queue_ahead
from .exits import book_reasons, evaluate_lot


class ResearchExecutor(Executor):
    lanes = [(f"{c}c-{proxy}", str(decimal(c)/100), proxy) for c in (1, 2, 3) for proxy in ("L0", "L1", "L2")]

    def __init__(self, store, tape_id, policy=Policy(), models=ModelConfig()):
        self.models = models
        self._ready = False
        self.version = "lesson6-v2:"+digest(asdict(models))
        self.current_frames = {}
        initialize(store.db)
        store.db.execute("CREATE TABLE IF NOT EXISTS l6_config(run_id TEXT PRIMARY KEY,payload TEXT)")
        super().__init__(store, tape_id, policy)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO l6_config VALUES(?,?)", (self.run_id, encode(asdict(models))))
        for lane, _, _ in self.lanes:
            self.ledger(lane).add(digest([self.run_id, lane, "capital"]), self.manifest["start_ms"], "CAPITAL", {"amount": policy.initial_cash, "virtual": True})
            self.ledger(lane).audit()
        self._ready = True
        self.audit()

    def ledger(self, lane):
        return Ledger(self.db, self.run_id, lane)

    def journal(self, lane, seq, at, kind, order=None, reason=None, evidence=None):
        if order and lane.endswith("L2") and "queue_ahead" not in order:
            frame = self.current_frames.get(order["asset_id"])
            if frame is None:
                raise ValueError("queue must be estimated from the current acceptance frame")
            order.update(queue_ahead=str(queue_ahead(order, frame, self.models)), queue_initial_ms=at,
                         queue_initial_book_ref=frame["book_ref"], queue_confidence="very_low_uncalibrated")
        super().journal(lane, seq, at, kind, order, reason, evidence)
        if order:
            q = order_quantities(order)
            payload = {**q, "asset_id": order["asset_id"], "market_id": order["market_id"], "time_ms": at, "last_reason": reason}
            self.db.execute("INSERT INTO l6_order_status VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,submitted_quantity=excluded.submitted_quantity,filled_quantity=excluded.filled_quantity,unfilled_quantity=excluded.unfilled_quantity,working_quantity=excluded.working_quantity,cancelled_quantity=excluded.cancelled_quantity,expired_quantity=excluded.expired_quantity,payload=excluded.payload",
                            (order["id"], self.run_id, lane, order["state"], *(q[k] for k in ("submitted_quantity", "filled_quantity", "unfilled_quantity", "working_quantity", "cancelled_quantity", "expired_quantity")), encode(payload)))

    def fill(self, lane, seq, at, order, qty, evidence):
        proxy = lane.split("-")[-1]
        frame = self.current_frames[order["asset_id"]]
        fee_rate, price = decimal(self.models.maker_fee_rate), decimal(order["price"])
        if order["side"] == "BUY":
            others = sum((decimal(o["price"])*decimal(o["remaining"])*(1+fee_rate) for o in self.orders(lane) if o["id"] != order["id"] and o["state"] in OPEN and o["side"] == "BUY"), decimal(0))
            affordable = max(decimal(0), decimal(self.account(lane)["cash"])-others)/(price*(1+fee_rate))
            qty = min(qty, grid(affordable, decimal(".01"), "BUY"))
        rich_evidence = {**evidence, "model": proxy, "assumptions": ASSUMPTIONS[proxy], "fee_assumption": self.models.maker_fee_rate,
                         "input_seq": seq, "decision_id": order["decision_id"], "frame": frame}
        used = super().fill(lane, seq, at, order, qty, rich_evidence)
        if not used:
            return used
        fee = used*price*fee_rate
        fill_id = digest([order["id"], seq, rich_evidence])
        state = self.ledger(lane).state()
        meta = state["assets"][order["asset_id"]]
        self.ledger(lane).add(fill_id, at, "FILL", {"order_id": order["id"], "asset_id": order["asset_id"], "market_id": order["market_id"],
            "role": meta["role"], "label": meta["label"], "complement": meta["complement"], "side": order["side"], "price": str(price), "quantity": str(used), "fee": str(fee),
            "purpose": "ENTRY" if order["side"] == "BUY" else "EXIT", "markout_anchor": fill_id,
            "context": {"fair_probability": frame["signal"]["fair_probability"], "quote_ms": order["created_ms"], "reference_sources": frame["signal"]["sources"],
                        "stale_quote": bool(hard_risk(frame, self.policy)), "model": proxy, "assumptions": ASSUMPTIONS[proxy], "book_ref": frame["book_ref"], "input_seq": seq,
                        "expected_hedge_price": self.current_frames.get(meta["complement"], {}).get("book", {}).get("ask")}, "evidence": rich_evidence})
        if fee:
            account = self.account(lane)
            account["cash"] = str(decimal(account["cash"])-fee)
            self.db.execute("UPDATE qe_accounts SET payload=? WHERE run_id=? AND lane=?", (encode(account), self.run_id, lane))
            self.journal(lane, seq, at, "SIMULATED_FEE", reason=self.models.fee_source, evidence={"fill_id": fill_id, "cash_fee": str(fee)})
        return used

    def proxy_fills(self, lane, proxy, seq, event, frames):
        if proxy != "L2":
            return super().proxy_fills(lane, proxy, seq, event, frames)
        trades = []
        for trade in event["trades"]:
            if self.db.execute("INSERT OR IGNORE INTO qe_consumed_trades VALUES(?,?,?,?)", (self.run_id, lane, trade["event_key"], trade["index"])).rowcount:
                trades.append(trade)
        for trade in trades:
            p, at = trade["payload"], event["time_ms"]
            try:
                frame = frames[p["asset_id"]]
                price, available = decimal(p["price"]), decimal(p["size"])
                if hard_risk(frame, self.policy) or p.get("market") != frame["market"]["condition_id"] or not 0 < price < 1 or available <= 0 or not 0 <= at-int(p["timestamp"]) <= frame["signal"]["config"]["book_max_age_ms"]:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            for order in sorted(self.orders(lane), key=lambda o: (o["created_ms"], o["id"])):
                if order["state"] not in OPEN or order["asset_id"] != p["asset_id"] or order["created_ms"] >= at:
                    continue
                eligible = (order["side"] == "BUY" and p.get("side") == "SELL" and price <= decimal(order["price"])) or (order["side"] == "SELL" and p.get("side") == "BUY" and price >= decimal(order["price"]))
                if not eligible:
                    continue
                ahead = decimal(order["queue_ahead"])
                consumed = min(ahead, available)
                order["queue_ahead"] = str(ahead-consumed)
                available -= consumed
                evidence = {"proxy": "L2_QUEUE_AWARE", "trade": trade, "queue_before": str(ahead), "queue_consumed": str(consumed), "queue_after": order["queue_ahead"], "queue_initial_book_ref": order["queue_initial_book_ref"]}
                self.journal(lane, seq, at, "QUEUE_PROGRESS", order.copy(), "eligible_print_volume_only", evidence)
                if available > 0:
                    available -= self.fill(lane, seq, at, order, available, evidence)

    def step(self, row):
        event = json.loads(row["payload"])
        self.current_frames = {f["asset_id"]: f for f in event["frames"]}
        at, seq = event["time_ms"], row["seq"]
        for lane, _, _ in self.lanes:
            ledger = self.ledger(lane)
            known = {l["asset_id"] for l in ledger.state()["lots"] if l.get("seed")}
            for f in event["frames"]:
                asset = f["asset_id"]
                if asset in known:
                    continue
                outcomes = f["market"]["outcomes"]
                ids = [o["asset_id"] for o in outcomes]
                if asset not in ids or len(set(ids)) != 2:
                    raise ValueError("binary asset mapping unavailable; refusing inventory seed")
                index = ids.index(asset)
                native = {o.get("label", "").lower() for o in outcomes} == {"yes", "no"}
                for i, outcome in enumerate(outcomes):
                    mapping = {"asset_id": outcome["asset_id"], "market_id": f["market_id"], "role": outcome["label"].upper() if native else "YES" if i == 0 else "NO", "label": outcome.get("label", outcome["asset_id"]), "complement": ids[1-i]}
                    existing = ledger.state()["assets"].get(outcome["asset_id"])
                    if existing:
                        if existing != {k: v for k, v in mapping.items() if k != "asset_id"}:
                            raise ValueError("market binary identity changed")
                    else:
                        ledger.add(digest([self.run_id, lane, outcome["asset_id"], "asset_mapping"]), at, "ASSET", mapping)
                price = (decimal(f["book"]["bid"])+decimal(f["book"]["ask"]))/2 if not book_reasons(f, at) else None
                if decimal(self.policy.initial_inventory) == 0:
                    price = decimal(0)
                ledger.add(digest([self.run_id, lane, asset, "seed"]), at, "SEED", {"asset_id": asset, "market_id": f["market_id"], "role": outcomes[index]["label"].upper() if native else "YES" if index == 0 else "NO",
                    "label": outcomes[index].get("label", asset), "complement": ids[1-index], "quantity": self.policy.initial_inventory, "price": str(price) if price is not None else None,
                    "context": {"fair_probability": str(price) if price is not None else None, "seed_basis": "first_observed_mid_assumed_capital_contribution", "binary_role_basis": "native_yes_no" if native else "outcome_index_not_native_yes_no_label"}})
        super().step(row)
        marks = {a: str((decimal(f["book"]["bid"])+decimal(f["book"]["ask"]))/2) for a, f in self.current_frames.items() if not book_reasons(f, at)}
        for lane, _, _ in self.lanes:
            ledger, orders = self.ledger(lane), self.orders(lane)
            state = ledger.state()
            result = valuation(state, marks, orders)
            previous = self.db.execute("SELECT payload FROM l6_valuations WHERE run_id=? AND lane=? ORDER BY input_seq DESC LIMIT 1", (self.run_id, lane)).fetchone()
            prior_peak = json.loads(previous[0]).get("peak_conservative_committed_exposure", "0") if previous else "0"
            result["peak_conservative_committed_exposure"] = str(max(decimal(prior_peak), decimal(result["conservative_committed_exposure"])))
            result.update(marks=marks, mark_evidence={a: {"book_ref": f["book_ref"], "received_ms": f["book_received_ms"]} for a, f in self.current_frames.items() if a in marks}, input_seq=seq, time_ms=at)
            self.db.execute("INSERT INTO l6_valuations VALUES(?,?,?,?,?)", (self.run_id, lane, seq, at, encode(result)))
            reserved = {}
            for order in orders:
                if order["state"] in OPEN and order["side"] == "SELL":
                    reserved[order["asset_id"]] = reserved.get(order["asset_id"], decimal(0))+decimal(order["remaining"])
            for lot in state["lots"]:
                if decimal(lot["remaining"]) <= 0:
                    continue
                reserved_lot = min(decimal(lot["remaining"]), reserved.get(lot["asset_id"], decimal(0)))
                reserved[lot["asset_id"]] = reserved.get(lot["asset_id"], decimal(0))-reserved_lot
                for candidate in evaluate_lot(lot, state, self.current_frames, at, self.models, str(reserved_lot), result["available_cash"]):
                    candidate["id"] = digest([self.run_id, lane, seq, candidate["id"]])
                    self.db.execute("INSERT INTO l6_candidates VALUES(?,?,?,?,?)", (candidate["id"], self.run_id, lane, at, encode(candidate)))

    def audit(self):
        result = super().audit()
        if getattr(self, "_ready", False):
            for lane, _, _ in self.lanes:
                ledger, account = self.ledger(lane), self.account(lane)
                ledger.audit()
                state = ledger.state()
                if decimal(state["cash"]) != decimal(account["cash"]) or any(balance(state, a) != decimal(q) for a, q in account["positions"].items()):
                    raise ValueError("FIFO ledger differs from virtual execution account")
            result["fifo_reconciled"] = True
        return result

    def run(self, max_frames=None, close=False):
        result = super().run(max_frames, close)
        if close:
            with self.db:
                for lane, _, _ in self.lanes:
                    row = self.db.execute("SELECT * FROM l6_valuations WHERE run_id=? AND lane=? ORDER BY input_seq DESC LIMIT 1", (self.run_id, lane)).fetchone()
                    if row:
                        p = json.loads(row["payload"])
                        p["exposure_before_close"] = str(max(decimal(p.get("exposure_before_close", "0")), decimal(p["conservative_committed_exposure"])))
                        p.update(reserved_cash="0", available_cash=p["cash"], conservative_committed_exposure=str(sum((decimal(q) for q in p["positions"].values()), decimal(0))), run_closed=True)
                        p.update(reserved_inventory={a: "0" for a in p["positions"]}, available_inventory=p["positions"])
                        self.db.execute("UPDATE l6_valuations SET payload=? WHERE run_id=? AND lane=? AND input_seq=?", (encode(p), self.run_id, lane, row["input_seq"]))
        return result

    def record_paper_event(self, lane, event):
        """Import an explicit synthetic accounting scenario, never submit an order/merge/redeem."""
        if lane not in {l for l, _, _ in self.lanes} or event["kind"] not in {"FILL", "HEDGE_INTENT", "PAIR", "SETTLEMENT", "REWARD"}:
            raise ValueError("unsupported paper accounting scenario")
        run = self.db.execute("SELECT * FROM qe_runs WHERE id=?", (self.run_id,)).fetchone()
        if run["state"] != "CLOSED" or event["time_ms"] < self.manifest["end_ms"]:
            raise ValueError("manual accounting requires a closed run and cannot rewrite its history")
        if event.get("data_kind") != "synthetic":
            raise ValueError("manual scenario must explicitly be synthetic; real receipts need a verified adapter")
        last = self.db.execute("SELECT payload FROM qe_input WHERE tape_id=? AND seq=?", (self.tape_id, run["cursor"])).fetchone()
        frames = {f["asset_id"]: f for f in json.loads(last[0])["frames"]} if last else {}
        at, ledger = event["time_ms"], self.ledger(lane)
        with self.db, localcontext() as ctx:
            ctx.prec = PRECISION
            if not ledger.add(event["id"], at, event["kind"], event["payload"]):
                return False
            state = ledger.state()
            account = {"cash": state["cash"], "positions": {a: str(balance(state, a)) for a in state["assets"]}}
            self.db.execute("UPDATE qe_accounts SET payload=? WHERE run_id=? AND lane=?", (encode(account), self.run_id, lane))
            self.journal(lane, run["cursor"], at, "PAPER_ACCOUNT_EVENT", reason=event["kind"], evidence={"event_id": event["id"], "synthetic": True, "no_order_submitted": True})
            marks = {a: str((decimal(f["book"]["bid"])+decimal(f["book"]["ask"]))/2) for a, f in frames.items() if not book_reasons(f, at)}
            result = valuation(state, marks)
            sequence = self.db.execute("SELECT coalesce(max(input_seq),0)+1 FROM l6_valuations WHERE run_id=? AND lane=?", (self.run_id, lane)).fetchone()[0]
            result.update(marks=marks, input_seq=sequence, time_ms=at, accounting_event=event["id"], asof_marks_from_last_processed_frame=True)
            self.db.execute("INSERT INTO l6_valuations VALUES(?,?,?,?,?)", (self.run_id, lane, sequence, at, encode(result)))
        self.audit()
        return True
