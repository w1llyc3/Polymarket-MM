"""Export bounded public sports field evidence; no guessed upstream timing."""
import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path


def collect(source, result, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(source).resolve().as_uri()+"?mode=ro", uri=True)
    try:
        rows = [{"id": r[0], "receive_ms": r[1], "processed_ms": r[2], "raw": json.loads(r[3]), "payload_sha256": hashlib.sha256(r[3].encode()).hexdigest()} for r in db.execute("SELECT id,receive_ms,processed_ms,payload FROM sports_raw ORDER BY id")]
        types = dict(db.execute("SELECT event_type,count(*) FROM raw_events GROUP BY event_type"))
        metadata = [json.loads(r[0]) for r in db.execute("SELECT metadata FROM market_versions ORDER BY id LIMIT 1")]
        book_samples = [json.loads(r[0]) for r in db.execute("SELECT payload FROM book_observations ORDER BY id LIMIT 2")]
    finally:
        db.close()
    fields = [
        ("gameId", "sport source game identity; requires explicit mapping to Gamma event", "UNKNOWN -> NO_QUOTE"),
        ("leagueAbbreviation", "league identity; non-NBA never authorizes NBA", "NO_QUOTE"),
        ("homeTeam", "home identity, must match market roles", "NO_QUOTE"),
        ("awayTeam", "away identity, must match market roles", "NO_QUOTE"),
        ("status", "game state; Suspended != stopped game clock != market suspension", "UNKNOWN -> cancel / HALT if risk held"),
        ("live", "source live flag, not proof of trustworthy latency", "UNKNOWN -> NO_QUOTE"),
        ("ended", "source final flag; true closes persistently", "UNKNOWN -> NO_QUOTE"),
        ("period", "sport-specific period, requires NBA Q/OT semantics", "UNKNOWN -> NO_QUOTE"),
        ("elapsed", "format alone does not prove remaining-in-period or clock running", "UNKNOWN -> NO_QUOTE"),
        ("finishedAt", "finish time if present; never per-update generation time", "does not satisfy upstream freshness"),
        ("upstream_ms", "required source-update ms; not documented sports field", "UNKNOWN upstream age -> NO_QUOTE"),
        ("paused", "required explicit trustworthy pause evidence; not documented bool field", "UNKNOWN -> NO_QUOTE"),
    ]
    with (out/"sports-field-contract.csv").open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["field", "actual_present_rows", "total_rows", "example_actual_raw", "semantics", "update_guarantee", "verifiable_freshness", "missing_action"])
        for field, semantics, action in fields:
            present = [r for r in rows if field in r["raw"]]
            writer.writerow([field, len(present), len(rows), json.dumps(present[0]["raw"][field]) if present else "ABSENT", semantics,
                             "No per-game <=3s update SLA established; heartbeat is transport only", "local callback/processing only; upstream age UNKNOWN", action])
    proof = {"source": "wss://sports-api.polymarket.com/ws", "documentation": "https://docs.polymarket.com/market-data/realtime-data", "raw_rows": rows,
             "nba_rows": sum(r["raw"].get("leagueAbbreviation") == "NBA" for r in rows), "raw_market_event_counts": types,
             "not_a_nba_clock_calibration": True, "source_database": str(source), "auto_contract_accepted": False}
    (out/"sports-raw-evidence.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    (out/"market-input-sample.json").write_text(json.dumps({"provenance": "real public metadata and first REST book observations; representative not full window", "markets": metadata, "book_samples": book_samples}, indent=2), encoding="utf-8")
    raw = Path(result).read_bytes()
    text = raw.decode("utf-16") if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode("utf-8-sig")
    (out/"real-run.json").write_text(json.dumps(json.loads(text), indent=2), encoding="utf-8")
    print(json.dumps({"sports_rows": len(rows), "nba_rows": proof["nba_rows"], "auto_contract_accepted": False}))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    collect(a.source, a.result, a.out)
