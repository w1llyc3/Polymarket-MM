"""Durable local exchange authority with a controllable virtual clock."""
import json

from ..fair_value.maths import decimal
from ..storage import encode


class PaperExchangeAdapter:
    mode = "PAPER"
    real_submission_calls = 0

    def __init__(self, db):
        self.db = db

    def authoritative(self, order_id):
        row = self.db.execute("SELECT payload FROM pe_exchange WHERE id=?", (order_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def balance_snapshot(self, seed_cash, seed_positions):
        """Independent paper fill-ledger reducer; never reads OMS fills/account."""
        cash = decimal(seed_cash)
        positions = {a: decimal(q) for a, q in seed_positions.items()}
        for row in self.db.execute("SELECT order_id,size FROM pe_fills ORDER BY rowid"):
            order = self.authoritative(row["order_id"])
            if not order or order["asset_id"] not in positions:
                return None
            qty = decimal(row["size"])
            direction = 1 if order["side"] == "BUY" else -1
            positions[order["asset_id"]] += direction*qty
            cash -= direction*qty*decimal(order["price"])
        return {"cash": cash, "positions": positions}

    def schedule(self, due, kind, order_id, payload):
        self.db.execute("INSERT INTO pe_pending(due_ms,kind,order_id,payload) VALUES(?,?,?,?)", (due, kind, order_id, encode(payload)))

    def submit(self, order, at, response_ms=5, accepted=True, drop_response=False):
        if self.authoritative(order["id"]):
            raise ValueError("duplicate client intent; reconcile instead of resubmit")
        authority = {**order, "state": "OPEN" if accepted else "REJECTED", "filled": "0", "exchange_ms": at}
        self.db.execute("INSERT INTO pe_exchange VALUES(?,?)", (order["id"], encode(authority)))
        if not drop_response:
            self.schedule(at+response_ms, "REPORT", order["id"], {"authority": authority, "report_id": "submit:"+order["id"], "exchange_ms": at})

    def cancel(self, order_id, at, latency_ms, drop_response=False):
        self.schedule(at+latency_ms, "EXCHANGE_CANCEL", order_id, {"requested_ms": at, "drop_response": drop_response})

    def apply_cancel(self, order_id, at, payload):
        authority = self.authoritative(order_id)
        if authority and authority["state"] not in {"FILLED", "REJECTED", "CANCELLED"}:
            authority.update(state="CANCELLED", exchange_ms=at)
            self.db.execute("UPDATE pe_exchange SET payload=? WHERE id=?", (encode(authority), order_id))
        if not payload["drop_response"]:
            self.schedule(at, "REPORT", order_id, {"authority": authority, "report_id": f"cancel:{order_id}:{payload['requested_ms']}", "exchange_ms": at, "cancel_requested_ms": payload["requested_ms"]})

    def fill(self, order_id, size, at, event_id, transport_ms=0):
        authority = self.authoritative(order_id)
        if not authority or authority["state"] not in {"OPEN", "PARTIALLY_FILLED"}:
            return False
        qty = decimal(size)
        if qty <= 0 or qty > decimal(authority["size"])-decimal(authority["filled"]):
            raise ValueError("invalid paper execution quantity")
        if not self.db.execute("INSERT OR IGNORE INTO pe_fills VALUES(?,?,?,?)", (event_id, order_id, at, str(qty))).rowcount:
            return False
        authority["filled"] = str(decimal(authority["filled"])+qty)
        authority.update(state="FILLED" if decimal(authority["filled"]) == decimal(authority["size"]) else "PARTIALLY_FILLED", exchange_ms=at)
        self.db.execute("UPDATE pe_exchange SET payload=? WHERE id=?", (encode(authority), order_id))
        self.schedule(at+transport_ms, "REPORT", order_id, {"authority": authority, "report_id": "fill:"+event_id, "exchange_ms": at})
        return True
