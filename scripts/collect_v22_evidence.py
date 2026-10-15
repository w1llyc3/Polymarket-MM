"""Reduce actual console exports into reviewable, token-free evidence."""
import argparse
import hashlib
import json
from pathlib import Path


def collect(path, destination):
    path, destination = Path(path), Path(destination)
    raw = path.read_bytes()
    bundle = json.loads(raw)
    view = bundle["view"]
    state = view["state"]
    summary = {"session_id": bundle["session_id"], "commit_recorded_at_server_start": bundle["commit"],
               "source_checkout": "v2.2 working tree on audited baseline; not a claim final code equals baseline", "exported_ms": bundle["exported_ms"],
               "source_export_sha256": hashlib.sha256(raw).hexdigest(), "source_export_filename": path.name,
               "mode": view["mode"], "lifecycle": view["lifecycle"], "version": view["version"],
               "books": state.get("books"), "account": state.get("account"), "phases": state.get("phases"),
               "execution_evidence": state.get("execution_evidence"), "watchdog_ticks": state.get("watchdog_ticks"),
               "completed_report": bundle["completed_report"], "real_submission_calls": 0}
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"session_id": summary["session_id"], "mode": summary["mode"], "summary": str(destination)}))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--export", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    collect(args.export, args.out)
