"""Read-only bridge from Lesson 5 paper authority to the FIFO accounting ledger."""
import json
from decimal import localcontext

from ..fair_value.maths import PRECISION, decimal
from ..fair_value.store import digest, read_connection
from ..paper_execution.adapter import PaperExchangeAdapter
from ..paper_execution.manager import OrderManager
from .exits import book_reasons
from .ledger import Ledger, balance, valuation
from .storage import initialize


def import_paper(store, source_path):
    initialize(store.db)
    source = read_connection(source_path)
    try:
        source.execute("BEGIN")
        control = source.execute("SELECT * FROM pe_control WHERE id=1").fetchone()
        if not control or control["mode"] != "PAPER":
            raise ValueError("only Lesson 5 PAPER authority can be imported")
        auditor = OrderManager.__new__(OrderManager)
        auditor.db, auditor.adapter = source, PaperExchangeAdapter(source)
        if not auditor.consistent():
            raise ValueError("source paper authority/ledger not reconciled; import refused")
        journal = source.execute("SELECT * FROM pe_journal ORDER BY seq LIMIT 1").fetchone()
        run_id = digest(["lesson5-paper-import-v1", journal["time_ms"], json.loads(journal["payload"]), control["config"]])
        ledger = Ledger(store.db, run_id, "PAPER_AUTHORITY")
        initial = json.loads(control["account"])
        orders = {r["id"]: json.loads(r["payload"]) for r in source.execute("SELECT * FROM pe_orders")}
        events = []
        for row in source.execute("SELECT * FROM pe_journal WHERE kind='VIRTUAL_ASSET_SEED' ORDER BY time_ms,seq"):
            p = json.loads(row["payload"])
            asset = p["detail"]["asset_id"]
            frames = [o["frame"] for o in orders.values() if o["asset_id"] == asset and o["frame"]["time_ms"] <= row["time_ms"]]
            if not frames:
                raise ValueError("source seed lacks as-of market/book evidence")
            frame = min(frames, key=lambda f: f["time_ms"])
            events.append((row["time_ms"], 0, row["seq"], "SEED", {"asset": asset, "frame": frame, "quantity": json.loads(p["control"]["account"])["seed_positions"][asset]}))
        for row in source.execute("SELECT * FROM pe_fills ORDER BY time_ms,rowid"):
            order = orders[row["order_id"]]
            frame = order["frame"]
            if frame["time_ms"] > row["time_ms"]:
                raise ValueError("future quote context cannot explain a past fill")
            available = control["clock_ms"]
            for report in source.execute("SELECT due_ms,payload FROM pe_pending WHERE kind='REPORT' AND order_id=? AND done=1 ORDER BY due_ms", (row["order_id"],)):
                if json.loads(report["payload"]).get("report_id") == "fill:"+row["event_id"]:
                    available = report["due_ms"]
                    break
            event_key = digest([run_id, "FILL", row["event_id"]])
            old = store.db.execute("SELECT time_ms FROM l6_events WHERE id=?", (event_key,)).fetchone()
            events.append((old[0] if old else available, 1, row["event_id"], "FILL", {"row": dict(row), "order": order, "frame": frame}))
        with store.db, localcontext() as ctx:
            ctx.prec = PRECISION
            ledger.add(digest([run_id, "capital"]), journal["time_ms"], "CAPITAL", {"amount": initial["seed_cash"], "source": "Lesson5 local paper authority"})
            for at, _, source_id, kind, p in sorted(events, key=lambda e: (e[0], e[1], str(e[2]))):
                frame = p["frame"]
                outcomes = frame["market"]["outcomes"]
                ids = [o["asset_id"] for o in outcomes]
                native = {o.get("label", "").lower() for o in outcomes} == {"yes", "no"}
                for i, outcome in enumerate(outcomes):
                    if outcome["asset_id"] not in ledger.state()["assets"]:
                        ledger.add(digest([run_id, outcome["asset_id"], "mapping"]), at, "ASSET", {"asset_id": outcome["asset_id"], "market_id": frame["market_id"], "role": outcome["label"].upper() if native else "YES" if i == 0 else "NO", "label": outcome.get("label", outcome["asset_id"]), "complement": ids[1-i]})
                asset = p["asset"] if kind == "SEED" else p["order"]["asset_id"]
                meta = ledger.state()["assets"][asset]
                if kind == "SEED":
                    mark = (decimal(frame["book"]["bid"])+decimal(frame["book"]["ask"]))/2 if not book_reasons(frame, at) else None
                    if decimal(p["quantity"]) == 0:
                        mark = decimal(0)
                    payload = {"asset_id": asset, **meta, "quantity": p["quantity"], "price": str(mark) if mark is not None else None,
                               "context": {"seed_basis": "asof_seed_mid_assumed", "fair_probability": str(mark) if mark is not None else None}, "source_journal_seq": source_id}
                else:
                    order, row = p["order"], p["row"]
                    config = frame["signal"].get("config", {})
                    executed = row["time_ms"]
                    stale = executed-frame["book_received_ms"] > config.get("book_max_age_ms", 0) or any(executed-s[k] > config.get("odds_max_age_ms", 0) for s in frame["signal"].get("sources", []) if s.get("included") for k in ("updated_ms", "received_ms"))
                    payload = {"asset_id": asset, **meta, "order_id": order["id"], "side": order["side"], "price": order["price"], "quantity": row["size"], "fee": "0",
                               "purpose": "ENTRY" if order["side"] == "BUY" else "EXIT", "source_fill_id": row["event_id"], "source_authority": "Lesson5 SQLite PAPER",
                               "observed_ms": at, "exchange_time_ms": executed, "context": {"model": "PAPER_AUTHORITY_FAULT_SIMULATION", "fair_probability": frame["signal"].get("fair_probability"),
                               "stale_quote": stale, "confidence": "synthetic_local_authority_not_real_exchange", "frame": frame}}
                ledger.add(digest([run_id, kind, source_id]), at, kind, payload)
            ledger.audit()
            state = ledger.state()
            if decimal(state["cash"]) != decimal(initial["cash"]) or any(balance(state, a) != decimal(q) for a, q in initial["positions"].items()):
                raise ValueError("imported FIFO balance differs from source paper account")
        marks = {}
        frames = [o["frame"] for o in orders.values()]
        for row in source.execute("SELECT payload FROM pe_quotes ORDER BY time_ms"):
            frames.append(json.loads(row[0])["frame"])
        for frame in sorted(frames, key=lambda f: f["time_ms"]):
            if not book_reasons(frame, control["clock_ms"]):
                marks[frame["asset_id"]] = str((decimal(frame["book"]["bid"])+decimal(frame["book"]["ask"]))/2)
        return {"run_id": run_id, "lane": "PAPER_AUTHORITY", "source_state": control["state"], "source_reconciled": True, "fifo_reconciled": ledger.audit(), "account": valuation(ledger.state(), marks, orders.values()),
                "real_submission_calls": 0, "not_live_performance": True, "scope": "retrospective paper accounting, never a historical signal input"}
    finally:
        source.close()
