import argparse
import json
from pathlib import Path
from .engine import FaultInjectionEngine


def main():
    parser = argparse.ArgumentParser(description="Offline deterministic Paper fault acceptance; no network")
    parser.add_argument("--out", required=True, help="New evidence directory; never overwrite")
    args = parser.parse_args()
    directory = Path(args.out)
    if directory.exists():
        parser.error("choose a new evidence directory")
    directory.mkdir(parents=True)
    result = FaultInjectionEngine(directory).run()
    (directory/"stress-results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()
