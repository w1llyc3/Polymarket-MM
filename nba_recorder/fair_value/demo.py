"""Explicitly synthetic deterministic scenario; never inserts demo data into a live DB."""
from pathlib import Path

from ..storage import encode
from .engine import Config, evaluate
from .providers import OddsBatch, ProviderError
from .store import FairStore
from .markouts import update_markouts

AT = 1791954000000  # 2026-10-14 13:00:00 Asia/Shanghai
RULES = {"period": "full_game", "overtime": True, "outcomes": 2, "cancellation": "half_payout", "postponement": "wait_until_completed"}


def market():
    return {"venue": "polymarket.com", "season": "2026-27", "market_id": "synthetic-demo-market", "event_id": "synthetic-event",
            "condition_id": "synthetic-condition", "league": "nba", "market_type": "moneyline", "team_ordering": "away",
            "home_team": "det", "away_team": "bos", "home_away_evidence": "synthetic_fixture", "game_start_ms": AT + 3600000, "synthetic": True,
            "description": "Full game including any overtime periods. If the game is postponed this market will remain open until the game has been completed. If canceled entirely it resolves 50-50.",
            "status": {"active": True, "closed": False, "archived": False, "acceptingOrders": True, "enableOrderBook": True},
            "tick_size": "0.01", "minimum_order_size": "5", "outcomes": [{"label": "Pistons", "asset_id": "synthetic-home"}, {"label": "Celtics", "asset_id": "synthetic-away"}]}


def quote(source="synthetic-book-a", home="-110", away="-110", odds_format="american", updated=None):
    return {"source_id": source, "event_id": "synthetic-event", "league": "nba", "season": "2026-27", "home_team": "Detroit Pistons", "away_team": "Boston Celtics",
            "start_ms": AT + 3600000, "status": "pregame", "odds_format": odds_format, "home_odds": home, "away_odds": away,
            "outcome_count": 2, "updated_ms": updated if updated is not None else AT - 1000, "rules": RULES.copy(),
            "rule_evidence": {"source_url": "https://example.test/synthetic-rules-not-a-bookmaker", "verified_ms": AT - 2000}}


def observation(store, received, bid="0.45", ask="0.55", asset="synthetic-home", flags=None):
    s = {"market_id": "synthetic-demo-market", "asset_id": asset, "exchange_timestamp": received, "local_receive_timestamp": received,
         "bid": bid, "ask": ask, "bids": [{"price": bid, "size": "100"}], "asks": [{"price": ask, "size": "100"}],
         "flags": flags or [], "tick_size": "0.01", "minimum_order_size": "5"}
    event = {"event_type": "book", "market": "synthetic-condition", "asset_id": asset, "timestamp": str(received), "bids": s["bids"], "asks": s["asks"]}
    store.base.commit(event, "synthetic", received, [s])


def create_demo(path):
    if Path(path).exists():
        raise ProviderError("Demo DB already exists; choose another --demo-db path (no overwrite)")
    store = FairStore(path)
    try:
        store.base.markets([market()], AT - 5000)
        store.base.health(AT - 5000, "connected", "synthetic-demo")
        a, b = quote(), quote("synthetic-book-b", "2.0", "2.0", "decimal")
        store.ingest(OddsBatch("synthetic-json", AT - 500, {"data_kind": "synthetic", "quotes": [a, b]}, [a, b], "synthetic"))
        observation(store, AT - 100)
        observation(store, AT - 100, asset="synthetic-away")
        signals = evaluate(store.db, "synthetic-demo-market", AT)
        for s in signals:
            store.persist(s)
        for horizon in (10, 30, 60, 300):
            observation(store, AT + horizon * 1000, "0.46", "0.56")
            observation(store, AT + horizon * 1000, "0.44", "0.54", "synthetic-away")
        markouts = update_markouts(store, AT + 305000)
        return {"data_kind": "synthetic", "not_live_odds": True, "signals": signals, "markouts": markouts}
    finally:
        store.close()
