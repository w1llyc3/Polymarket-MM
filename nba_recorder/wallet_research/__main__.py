import argparse
import json
import time
from .collector import collect_wallet


def main():
    parser = argparse.ArgumentParser(description="Public wallet evidence only; no copy trading or quote integration")
    parser.add_argument("--address", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--end", type=int, help="Fixed epoch seconds; default captured once at start")
    parser.add_argument("--start", type=int, help="Epoch seconds; default one hour before end for first evidence sample")
    args = parser.parse_args()
    end = args.end if args.end is not None else int(time.time())
    start = args.start if args.start is not None else end-3600
    result = collect_wallet(args.address, args.out, start, end)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["complete_window"] else 1)


if __name__ == "__main__":
    main()
