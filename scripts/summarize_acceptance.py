"""Generate read-only strategy evidence, including availability and gross edge."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_recorder.stress.research import summarize

parser = argparse.ArgumentParser()
parser.add_argument("--db", required=True)
parser.add_argument("--run", required=True)
parser.add_argument("--out", required=True)
args = parser.parse_args()
result = summarize(args.db, args.run)
Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps({"days": len(result["reports"]), "out": args.out, "live_go": False}))
