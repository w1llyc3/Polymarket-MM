"""Bounded, offline planner comparison against the audited Git baseline.

Loads only baseline reference/session source from an explicit commit; uses the
current shared Paper OMS/FIFO in both lanes, isolating the planner change.
This is synthetic regression evidence, not a full historic service benchmark.
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_recorder.fair_value.store import digest
from nba_recorder.fair_value.maths import decimal
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.report import report

BASE = "1a1eb9b304b6e47ea7e75c623d6ecd75ba86748c"


def baseline(name, directory):
    raw = subprocess.check_output(["git", "show", BASE+":nba_recorder/inplay/"+name+".py"])
    path = Path(directory)/(name+".py")
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location("nba_recorder.inplay._audited_"+name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compare(out):
    stream = []
    for i in range(23):
        at = AT+i*30
        trades = [] if i != 22 else [{"event_id": "fixture-print", "asset_id": "a", "condition_id": "synthetic-condition", "side": "BUY", "price": ".53", "size": "2", "exchange_ms": at}]
        stream.append({"id": "same-fixture-"+str(i), "time_ms": at, "frames": [frame(at, a) for a in ("a", "b")], "trades": trades, "provenance": "synthetic regression"})
    results = []
    with tempfile.TemporaryDirectory() as temp:
        reference = baseline("reference", temp)
        old = baseline("session", temp)
        old.book_targets, old.book_risk, old.observed_mid = reference.book_targets, reference.book_risk, reference.observed_mid
        for label, cls in (("baseline_planner", old.BookSession), ("v2.1_planner", BookSession)):
            store = SingleWriterStore(Path(temp)/(label+".sqlite"))
            try:
                # Zero skew isolates self-reservation fix from directional skew.
                om = UnifiedPaperManager(store, BookPolicy(inventory_skew="0"), AT)
                om.split(stream[0]["frames"][0]["market"], "5", "same-cash-funded-split")
                session = cls(om, "L1")
                retained, opportunities = 0, 0
                for event in stream:
                    om.advance(event["time_ms"])
                    before = {o["id"] for o in om.orders() if o["state"] in {"OPEN", "PARTIALLY_FILLED"}}
                    session.on_event(event)
                    if before:
                        opportunities += len(before)
                        retained += sum(o["id"] in before and o["state"] in {"OPEN", "PARTIALLY_FILLED"} for o in om.orders())
                om.advance(om.now+3)
                r = report(om)
                quantities = [json.loads(v[0])["quantity"] for v in om.db.execute("SELECT payload FROM l6_events WHERE kind='FILL' AND run_id=?", (om.run_id,)) if json.loads(v[0]).get("purpose") != "SIMULATED_COMPLETE_SET_SPLIT"]
                results.append({"lane": label, "cancel_requests": store.db.execute("SELECT count(*) FROM pe_journal WHERE kind='CANCEL_REQUEST'").fetchone()[0],
                                "retained_order_observations": retained, "retention_opportunities": opportunities,
                                "retention_ratio": str(decimal(retained)/opportunities) if opportunities else None,
                                "orders_created": r["orders"], "simulated_fill_quantity": str(sum(map(decimal, quantities), decimal(0))),
                                "net_pnl": r["account"]["net_pnl"], "inventory": r["account"]["positions"], "consistent": r["ledger_and_oms_consistent"]})
            finally:
                store.close()
    evidence = {"baseline": BASE, "fixture_digest": digest(stream), "source": "synthetic; equal initial cash100/split5, L1, latency and stream; skew0 to isolate P0-A",
                "scope": "baseline reference/session vs repaired reference/session, shared current OMS/FIFO; not full baseline service", "stream": stream,
                "results": results, "not_real_profit": True, "real_submission_calls": 0}
    Path(out).write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    print(json.dumps(compare(p.parse_args().out), indent=2))
