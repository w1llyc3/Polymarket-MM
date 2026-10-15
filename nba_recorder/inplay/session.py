"""Unified asynchronous Paper session and causal L0/L1/L2 fill hypotheses."""
import json
from ..fair_value.maths import decimal
from ..fair_value.store import digest
from ..paper_execution.risk import ACTIVE
from ..storage import encode
from .reference import book_targets, book_risk, observed_mid


class BookSession:
    def __init__(self, manager, model="L2"):
        if model not in {"L0", "L1", "L2"}:
            raise ValueError("explicit fill model L0/L1/L2 required")
        with manager.db:
            old = manager.db.execute("SELECT payload FROM ip_session_config WHERE id='fill_model'").fetchone()
            if old and old[0] != model:
                raise ValueError("fill model is immutable across restarts; use a separate paper account")
            manager.db.execute("INSERT OR IGNORE INTO ip_session_config VALUES('fill_model',?)", (model,))
        self.om, self.model = manager, model

    def observe(self, frame):
        om = self.om
        om.advance(max(om.now, frame["time_ms"]))
        frame = om.phase_frame(frame)
        # phase supplied in input is never trusted as an OMS authorization.
        phase = om.phases.update(frame, om.now)
        frame = {**frame, "phase": phase["state"]}
        previous = om.db.execute("SELECT payload FROM pe_risk_frames WHERE asset_id=?", (frame["asset_id"],)).fetchone()
        old = json.loads(previous[0]) if previous else None
        previous_mid = observed_mid(old, om.policy, old["time_ms"]) if old else None
        om.book_risk_check(frame, previous_mid)
        if not om.halted:
            om.register_market(frame["market"])
        for order in om.orders():
            if order["asset_id"] == frame["asset_id"] and order["state"] in ACTIVE:
                if phase["state"] not in {"ACTIVE_MM", "REDUCE_ONLY"} or (phase["state"] == "REDUCE_ONLY" and order.get("purpose") != "RISK_EXIT"):
                    om.cancel(order["id"])
        return frame

    def quote(self, frame, input_id):
        om = self.om
        catalog = {a: {"event_id": e} for a, e in om.account()["events"].items()}
        decision = book_targets(frame, om.account(), om.orders(), catalog, om.policy)
        if om.halted:
            decision.update(state="NO_QUOTE", targets=[], reasons=["risk_control_"+om.risk_state])
        with om.db:
            om.db.execute("INSERT OR IGNORE INTO pe_quotes VALUES(?,?,?)", (input_id, om.now, encode({**decision, "frame": frame, "model": self.model})))
        wanted = {t["side"]: t for t in decision["targets"]}
        for order in om.orders():
            if order["asset_id"] != frame["asset_id"] or order["state"] not in ACTIVE:
                continue
            target = wanted.get(order["side"])
            if target and target.get("action") == "RETAIN" and target.get("retained_order_id") == order["id"]:
                wanted.pop(order["side"])
            elif order["state"] not in {"UNKNOWN", "RECONCILED", "CANCEL_PENDING"}:
                om.cancel(order["id"])
        busy = {o["side"] for o in om.orders() if o["asset_id"] == frame["asset_id"] and o["state"] in ACTIVE}
        for side, target in wanted.items():
            if side not in busy:
                om.enqueue(input_id+":"+side, target, frame)
        om.dispatch()
        # Initial visible queue is fixed using information available at dispatch.
        with om.db:
            for order in om.orders():
                if order["asset_id"] != frame["asset_id"] or om.db.execute("SELECT 1 FROM ip_models WHERE order_id=?", (order["id"],)).fetchone() or not om.adapter.authoritative(order["id"]):
                    continue
                levels = frame["book"]["bids" if order["side"] == "BUY" else "asks"]
                price = decimal(order["price"])
                ahead = sum((decimal(l["size"]) for l in levels if (decimal(l["price"]) >= price if order["side"] == "BUY" else decimal(l["price"]) <= price)), decimal(0))
                om.db.execute("INSERT INTO ip_models VALUES(?,?)", (order["id"], encode({"model": self.model, "ahead": str(ahead), "initialized_ms": om.now, "assumption": "initial visible depth+FIFO; no inferred cancels; new better arrivals unknown", "confidence": "uncalibrated"})))
        return decision

    def executions(self, frames, trades, input_id):
        om = self.om
        by_asset = {f["asset_id"]: f for f in frames}
        with om.db:
            for index, trade in enumerate(trades):
                key = trade.get("event_id")
                if not key:
                    continue  # no stable print identity, cannot allocate safely
                old = om.db.execute("SELECT payload FROM ip_prints WHERE id=?", (key,)).fetchone()
                if old:
                    if old[0] != encode(trade):
                        om._kill("print_identity_collision")
                    continue
                om.db.execute("INSERT INTO ip_prints VALUES(?,?)", (key, encode(trade)))
                try:
                    f = by_asset[trade["asset_id"]]
                    price, available = decimal(trade["price"]), decimal(trade["size"])
                    if trade.get("condition_id") != f["market"]["condition_id"] or trade.get("side") not in {"BUY", "SELL"} or not 0 < price < 1 or available <= 0 or not 0 <= om.now-trade["exchange_ms"] <= om.policy.book_max_age_ms:
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                flow = om.db.execute("SELECT * FROM ip_flow WHERE asset_id=?", (trade["asset_id"],)).fetchone()
                quantity = decimal(flow["quantity"])+available if flow and flow["side"] == trade["side"] and om.now-flow["first_ms"] <= 5000 else available
                first = flow["first_ms"] if flow and flow["side"] == trade["side"] and om.now-flow["first_ms"] <= 5000 else om.now
                om.db.execute("INSERT INTO ip_flow VALUES(?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET side=excluded.side,quantity=excluded.quantity,first_ms=excluded.first_ms", (trade["asset_id"], trade["side"], str(quantity), first))
                depth = sum((decimal(l["size"]) for l in (f.get("book") or {}).get("bids" if trade["side"] == "SELL" else "asks", [])), decimal(0))
                diagnostic = {"kind": "public_one_sided_flow", "quantity": str(quantity), "visible_side_depth": str(depth), "flow_depth_ratio": str(quantity/depth) if depth else None,
                              "previous_streak_quantity": flow["quantity"] if flow else None, "own_fill_quantity": str(sum((decimal(r[0]) for r in om.db.execute("SELECT quantity FROM ip_own_fills WHERE asset_id=? AND receipt_ms>=?", (trade["asset_id"], om.now-5000))), decimal(0))),
                              "risk_level": "DIAGNOSTIC", "threshold_calibrated": False}
                diagnostic["own_same_side_fills"] = str(sum((decimal(r[0]) for r in om.db.execute("SELECT quantity FROM ip_own_fills WHERE asset_id=? AND side=? AND receipt_ms>=?", (trade["asset_id"], "BUY" if trade["side"] == "SELL" else "SELL", om.now-5000))), decimal(0)))
                diagnostic["recent_volume_baseline"] = "UNAVAILABLE: previous streak is not calibrated market activity"
                om.db.execute("INSERT INTO ip_flow_diagnostics(time_ms,asset_id,payload) VALUES(?,?,?)", (om.now, trade["asset_id"], encode(diagnostic)))
                if quantity > decimal(om.policy.max_one_sided_shares):
                    om._risk("public_one_sided_flow_diagnostic", evidence=diagnostic)
                    if om.policy.public_flow_pause and not om.halted:
                        om.pause("explicit_public_flow_pause_policy")
                # Allocate one public print budget across queue and all local
                # orders, including cancellation-in-flight. Never use phase to
                # pretend an already resting order cannot fill after cutoff.
                orders = sorted(om.orders(), key=lambda o: (0 if o["side"] == "BUY" else 1, -decimal(o["price"]) if o["side"] == "BUY" else decimal(o["price"]), o.get("submit_ms", o["created_ms"]), o["id"]))
                for order in orders:
                    authority = om.adapter.authoritative(order["id"])
                    if authority and order["asset_id"] == trade["asset_id"] and trade["exchange_ms"] in {order.get("submit_ms"), authority.get("cancelled_ms")}:
                        om._log("SAME_MS_EXECUTION_AMBIGUOUS", order, {"trade_id": key, "exchange_ms": trade["exchange_ms"], "action": "exclude uncertain fill; no ordering evidence"})
                    eligible_at_exchange = authority and (authority["state"] in {"OPEN", "PARTIALLY_FILLED"} or (authority["state"] == "CANCELLED" and trade["exchange_ms"] < authority.get("cancelled_ms", -1)))
                    if not eligible_at_exchange or order["asset_id"] != trade["asset_id"] or order["frame"]["market"]["condition_id"] != trade["condition_id"] or order.get("submit_ms", om.now) >= trade["exchange_ms"]:
                        continue
                    matches = (order["side"] == "BUY" and trade["side"] == "SELL" and price <= decimal(order["price"])) or (order["side"] == "SELL" and trade["side"] == "BUY" and price >= decimal(order["price"]))
                    if not matches or self.model == "L0":
                        continue
                    if self.model == "L1" and price == decimal(order["price"]):
                        continue
                    queue = om.db.execute("SELECT payload FROM ip_models WHERE order_id=?", (order["id"],)).fetchone()
                    if self.model == "L2":
                        if not queue:
                            continue
                        q = json.loads(queue[0])
                        consume = min(decimal(q["ahead"]), available)
                        q["ahead"] = str(decimal(q["ahead"])-consume)
                        available -= consume
                        om.db.execute("UPDATE ip_models SET payload=? WHERE order_id=?", (encode(q), order["id"]))
                    qty = min(available, decimal(order["size"])-decimal(authority["filled"]))
                    if qty <= 0:
                        continue
                    fill_id = digest(["unified-fill", key, order["id"]])
                    if om.adapter.fill(order["id"], str(qty), trade["exchange_ms"], fill_id, om.policy.data_transport_ms, observed_ms=om.now):
                        available -= qty
                        fill_mid = observed_mid(f, om.policy, om.now) if trade["exchange_ms"] == om.now else None
                        if fill_mid is None:
                            for history in om.db.execute("SELECT payload FROM ip_inputs WHERE time_ms<=? ORDER BY time_ms DESC,rowid DESC", (trade["exchange_ms"],)):
                                past = next((v for v in json.loads(history[0])["frames"] if v["asset_id"] == order["asset_id"]), None)
                                if past:
                                    fill_mid = observed_mid(past, om.policy, trade["exchange_ms"])
                                    break
                        om.db.execute("INSERT INTO ip_fill_evidence VALUES(?,?)", (fill_id, encode({"trade_id": key, "model": self.model, "assumptions": "public aggressor side assumed, causal exchange time and volume budget; not actual execution", "confidence": "uncalibrated", "exchange_ms": trade["exchange_ms"], "strategy_available_ms": om.now, "reference_mid_at_fill": fill_mid})))
            if self.model == "L0":
                for order in om.orders():
                    authority = om.adapter.authoritative(order["id"])
                    f = by_asset.get(order["asset_id"])
                    if not authority or authority["state"] not in {"OPEN", "PARTIALLY_FILLED"} or not f or not isinstance(f.get("book"), dict) or not f.get("connected") or f["book"].get("flags") or order.get("submit_ms", om.now) >= f["time_ms"]:
                        continue
                    permitted_quality_reasons = {"phase_not_permitted", "late_close_threshold", "late_reduce_boundary", "unreliable_sports_state", "sports_stale_or_future", "invalid_clock"}
                    if set(book_risk(f, om.policy, om.now))-permitted_quality_reasons or f["market_id"] != order["market_id"]:
                        continue
                    try:
                        levels = f["book"]["asks" if order["side"] == "BUY" else "bids"]
                        top = decimal(levels[0]["price"])
                        touch = top <= decimal(order["price"]) if order["side"] == "BUY" else top >= decimal(order["price"])
                        if not touch or not 0 <= om.now-f["book_received_ms"] <= om.policy.book_max_age_ms:
                            continue
                        key = digest(["touch", order["id"], f.get("book_ref")])
                        qty = min(decimal(levels[0]["size"]), decimal(authority["size"])-decimal(authority["filled"]))
                        if om.adapter.fill(order["id"], str(qty), om.now, key, om.policy.data_transport_ms):
                            om.db.execute("INSERT OR IGNORE INTO ip_fill_evidence VALUES(?,?)", (key, encode({"model": "L0", "confidence": "very_low", "assumption": "touch is potential fill only", "reference_mid_at_fill": str((decimal(f["book"]["bid"])+decimal(f["book"]["ask"]))/2)})))
                    except (KeyError, ValueError, IndexError):
                        continue

    def on_event(self, event):
        with self.om.db:
            return self._on_event(event)

    def _on_event(self, event):
        om = self.om
        identity = event["id"]
        old = om.db.execute("SELECT payload FROM ip_inputs WHERE id=?", (identity,)).fetchone()
        if old:
            if old[0] != encode(event):
                raise ValueError("immutable stream identity collision")
            return False
        if event["time_ms"] < om.now:
            raise ValueError("event earlier than processed paper clock")
        if any(f["time_ms"] != event["time_ms"] or (type(f.get("received_ms")) is int and f["received_ms"] > event["time_ms"]) or (type(f.get("processed_ms")) is int and f["processed_ms"] > event["time_ms"]) for f in event["frames"]):
            raise ValueError("future frame cannot enter current paper event")
        om.advance(event["time_ms"])
        frames = [self.observe(f) for f in event["frames"]]
        if event.get("kind") == "WATCHDOG":
            if not frames and any(o["state"] in ACTIVE for o in om.orders()):
                om.kill("watchdog_no_valid_inputs")
            if frames:
                om.mark_equity(frames)
            with om.db:
                om.db.execute("INSERT INTO ip_inputs VALUES(?,?,?)", (identity, event["time_ms"], encode(event)))
                om._log("WALL_CLOCK_WATCHDOG", detail={"input_id": identity, "wall_ms": event.get("wall_ms"), "new_quotes_allowed": False})
            return True
        # Existing orders can fill in the same frame that requests a cancel.
        self.executions(frames, event.get("trades", []), identity)
        for index, f in enumerate(frames):
            self.quote(f, identity+":"+str(index))
        om.advance(om.now)
        # Require a fresh mark for every held asset; no odds are consulted.
        if frames:
            om.mark_equity(frames)
        with om.db:
            om.db.execute("INSERT INTO ip_inputs VALUES(?,?,?)", (identity, event["time_ms"], encode(event)))
            om._log("UNIFIED_INPUT_COMMITTED", detail={"input_id": identity, "model": self.model})
        return True
