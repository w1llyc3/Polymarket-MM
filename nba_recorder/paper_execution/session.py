"""Bridge pure quote targets to durable paper intents, without network calls."""
import json

from ..fair_value.maths import decimal
from ..fair_value.store import digest
from ..quote_engine.model import OPEN, hard_risk, targets
from ..storage import encode
from .risk import ACTIVE


class PaperSession:
    def __init__(self, manager, min_edge="0.01"):
        self.manager, self.min_edge = manager, min_edge

    def on_frame(self, frame, identity):
        om = self.manager
        om.advance(max(om.now, frame["time_ms"]))
        previous = om.db.execute("SELECT mid FROM pe_reference WHERE asset_id=? AND time_ms<=?", (frame["asset_id"], frame["time_ms"])).fetchone()
        om.risk_check(frame, previous_mid=previous[0] if previous else None)
        with om.db:
            if not hard_risk(frame, om.policy):
                mid = str((decimal(frame["book"]["bid"])+decimal(frame["book"]["ask"]))/2)
                om.db.execute("INSERT INTO pe_reference VALUES(?,?,?) ON CONFLICT(asset_id) DO UPDATE SET time_ms=excluded.time_ms,mid=excluded.mid", (frame["asset_id"], frame["time_ms"], mid))
            if not om.halted:
                om.seed_asset(frame["asset_id"], frame["market"]["event_id"])
            account = om.account()
            catalog = {a: {"event_id": e} for a, e in account["events"].items()}
            orders = [{**o, "state": "OPEN", "remaining": str(decimal(o["size"])-decimal(o["filled"]))} for o in om.orders() if o["state"] in ACTIVE]
            decision = targets(frame, account, orders, catalog, self.min_edge, om.policy) if frame["asset_id"] in catalog else {"state": "NO_QUOTE", "targets": [], "reasons": ["halted_or_missing_asset"]}
            if om.halted:
                decision.update(state="NO_QUOTE", targets=[], reasons=["kill_switch_halted"]+decision.get("reasons", []))
            decision.update(id=identity, frame=frame, observed_ms=frame["time_ms"], managed_ms=om.now, virtual=True, real_submission_calls=0)
            om.db.execute("INSERT OR IGNORE INTO pe_quotes VALUES(?,?,?)", (identity, om.now, encode(decision)))
        wanted = {t["side"]: t for t in decision["targets"]}
        active = [o for o in om.orders() if o["state"] in ACTIVE and o["asset_id"] == frame["asset_id"]]
        for order in active:
            target = wanted.get(order["side"])
            if target and order["state"] in OPEN and decimal(order["price"]) == decimal(target["price"]) and decimal(order["size"])-decimal(order["filled"]) == decimal(target["size"]):
                wanted.pop(order["side"])
            elif order["state"] not in {"UNKNOWN", "RECONCILED", "CANCEL_PENDING"}:
                om.cancel(order["id"])
        # Cancel/replace waits for acknowledgement; commitment is retained while pending.
        busy_sides = {o["side"] for o in om.orders() if o["asset_id"] == frame["asset_id"] and o["state"] in ACTIVE}
        for side, target in wanted.items():
            if side not in busy_sides:
                om.enqueue(identity+":"+side, target, frame, self.min_edge)
        om.dispatch()
        with om.db:
            om.db.execute("INSERT OR IGNORE INTO pe_frame_done VALUES(?)", (identity,))
        return decision

    def replay(self, tape_store, tape_id, max_frames=None):
        tape_store.verify(tape_id)
        om = self.manager
        with om.db:
            other = om.db.execute("SELECT tape_id FROM pe_stream WHERE tape_id<>?", (tape_id,)).fetchone()
            if other:
                raise ValueError("one immutable tape per paper execution account")
            om.db.execute("INSERT OR IGNORE INTO pe_stream VALUES(?,0)", (tape_id,))
        cursor = om.db.execute("SELECT cursor FROM pe_stream WHERE tape_id=?", (tape_id,)).fetchone()[0]
        rows = tape_store.db.execute("SELECT * FROM qe_input WHERE tape_id=? AND seq>? ORDER BY seq", (tape_id, cursor)).fetchall()
        if max_frames is not None:
            rows = rows[:max_frames]
        for row in rows:
            event = json.loads(row["payload"])
            # Intents use stable per-frame IDs; crash before cursor commit never submits twice.
            for f in event["frames"]:
                identity = digest([tape_id, row["seq"], f["asset_id"], self.min_edge])
                if not om.db.execute("SELECT 1 FROM pe_frame_done WHERE id=?", (identity,)).fetchone():
                    self.on_frame(f, identity)
            frames = {f["asset_id"]: f for f in event["frames"]}
            for trade in event["trades"]:
                p = trade["payload"]
                trade_id = digest([tape_id, trade["event_key"], trade["index"]])
                if om.db.execute("SELECT 1 FROM pe_trade_processed WHERE id=?", (trade_id,)).fetchone():
                    continue
                try:
                    f = frames[p["asset_id"]]
                    price, available = decimal(p["price"]), decimal(p["size"])
                    if hard_risk(f, om.policy) or not 0 < price < 1 or available <= 0 or p.get("market") != f["market"]["condition_id"] or not 0 <= event["time_ms"]-int(p["timestamp"]) <= f["signal"]["config"]["book_max_age_ms"]:
                        continue
                except (ValueError, KeyError, TypeError):
                    continue
                with om.db:
                    for order in om.orders():
                        authority = om.adapter.authoritative(order["id"])
                        if not authority or authority["state"] not in {"OPEN", "PARTIALLY_FILLED"} or order["asset_id"] != p["asset_id"] or order["created_ms"] >= event["time_ms"]:
                            continue
                        through = (order["side"] == "BUY" and p.get("side") == "SELL" and price < decimal(order["price"])) or (order["side"] == "SELL" and p.get("side") == "BUY" and price > decimal(order["price"]))
                        qty = min(available, decimal(authority["size"])-decimal(authority["filled"]))
                        if through and qty > 0:
                            event_id = digest([trade_id, order["id"]])
                            if om.adapter.fill(order["id"], str(qty), om.now, event_id, om.policy.data_transport_ms):
                                available -= qty
                                om._log("PAPER_PROXY_EXECUTION", order, {"trade": trade, "size": str(qty), "proxy": "L1_TRADE_THROUGH_NOT_REAL_FILL"})
                    om.db.execute("INSERT INTO pe_trade_processed VALUES(?)", (trade_id,))
            om.advance(max(om.now, event["time_ms"]))
            om.mark_equity(event["frames"])
            with om.db:
                om.db.execute("UPDATE pe_stream SET cursor=? WHERE tape_id=?", (row["seq"], tape_id))
                om._log("TAPE_CURSOR", detail={"tape_id": tape_id, "seq": row["seq"]})
        return om.status()
