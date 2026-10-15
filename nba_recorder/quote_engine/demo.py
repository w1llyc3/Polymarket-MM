"""Synthetic fixtures, clearly separated from real recorded market data."""
from pathlib import Path

from ..fair_value.demo import AT, market, observation, quote
from ..fair_value.providers import OddsBatch
from ..fair_value.store import FairStore
from .executor import Executor
from .model import Policy
from .storage import PaperStore
from .tape import freeze


def demo(source, destination):
    if Path(source).exists() or Path(destination).exists():
        raise ValueError("demo refuses to overwrite; choose new paths")
    s = FairStore(source)
    try:
        s.base.markets([market()], AT-5000)
        s.base.health(AT-5000, "connected", "synthetic")
        for offset in range(0, 351000, 1000):
            at = AT+offset
            if offset % 30000 == 0:
                qs = [quote(updated=at), quote("synthetic-book-b", updated=at)]
                s.ingest(OddsBatch("synthetic", at, {"data_kind": "synthetic", "quotes": qs}, qs, "synthetic"))
            if offset % 60000 == 0:
                s.base.markets([market()], at)
            bid, ask = ("0.48", "0.49") if offset == 10000 else ("0.52", "0.53") if offset == 12000 else ("0.49", "0.51")
            observation(s, at, bid, ask)
            observation(s, at, "0.49", "0.51", "synthetic-away")
            if offset in (15000, 17000):
                s.base.health(at, "disconnected" if offset == 15000 else "connected", "synthetic-gap")
            if offset in (10000, 12000):
                t = at+500
                trade = {"event_type": "last_trade_price", "asset_id": "synthetic-home", "market": "synthetic-condition", "side": "SELL" if offset == 10000 else "BUY", "price": "0.47" if offset == 10000 else "0.54", "size": "2", "timestamp": str(t)}
                s.base.commit(trade, "synthetic", t, [])
    finally:
        s.close()
    p = PaperStore(destination)
    try:
        tape = freeze(p, source, AT, AT+350000)
        executor = Executor(p, tape, Policy(initial_inventory="10", target_inventory="10"))
        executor.run(close=True)
        return {**executor.audit(), "tape": tape, "data_kind": "synthetic", "source": source, "db": destination}
    finally:
        p.close()
