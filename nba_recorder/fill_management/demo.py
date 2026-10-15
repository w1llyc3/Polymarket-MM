"""Small synthetic research stream with deliberately different model outcomes."""
from pathlib import Path

from ..fair_value.demo import AT, market, quote
from ..fair_value.providers import OddsBatch
from ..fair_value.store import FairStore


def create_source(path):
    if Path(path).exists():
        raise ValueError("synthetic source already exists; choose a new path")
    store = FairStore(path)
    try:
        store.base.health(AT-1000, "connected", "synthetic_lesson6")
        for offset in range(0, 351000, 1000):
            at = AT+offset
            if offset % 30000 == 0:
                qs = [quote(updated=at), quote("synthetic-book-b", updated=at)]
                store.ingest(OddsBatch("synthetic", at, {"data_kind": "synthetic", "quotes": qs}, qs, "synthetic"))
            if offset % 60000 == 0:
                store.base.markets([market()], at)
            for asset in ("synthetic-home", "synthetic-away"):
                bid, ask = (".48", ".49") if offset == 10000 and asset == "synthetic-home" else (".52", ".53") if offset == 12000 and asset == "synthetic-home" else (".49", ".51")
                book = {"market_id": "synthetic-demo-market", "asset_id": asset, "exchange_timestamp": at, "local_receive_timestamp": at,
                        "bid": bid, "ask": ask, "bids": [{"price": bid, "size": "4"}], "asks": [{"price": ask, "size": "100" if offset == 10000 else "4"}],
                        "flags": [], "tick_size": ".01", "minimum_order_size": "5"}
                raw = {"event_type": "book", "market": "synthetic-condition", "asset_id": asset, "timestamp": str(at), "bids": book["bids"], "asks": book["asks"]}
                store.base.commit(raw, "synthetic", at, [book])
            if offset in (10000, 12000):
                t = at+500
                trade = {"event_type": "last_trade_price", "asset_id": "synthetic-home", "market": "synthetic-condition", "side": "SELL" if offset == 10000 else "BUY", "price": ".47" if offset == 10000 else ".54", "size": "6", "timestamp": str(t)}
                store.base.commit(trade, "synthetic", t, [])
            if offset in (15000, 17000):
                store.base.health(at, "disconnected" if offset == 15000 else "connected", "synthetic_gap")
    finally:
        store.close()
