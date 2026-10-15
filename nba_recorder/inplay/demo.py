"""Explicit synthetic in-play fixtures, never a live game simulation claim."""
from ..stress.engine import AT, fixture


def frame(at=AT, asset="a", period=2, remaining=600):
    f = fixture(at, asset)
    f.pop("signal")
    f.update(strategy_mode="BOOK_DRIVEN", market_observed_ms=at, received_ms=at, processed_ms=at,
             backlog_ms=0, phase="UNKNOWN")
    f["market"].update(event_live=True, outcomes=[{"asset_id": "a", "label": "Pistons"}, {"asset_id": "b", "label": "Celtics"}])
    f["sports"] = {"event_id": f["market"]["event_id"], "game_id": "synthetic-game", "source": "synthetic_verified_clock_fixture",
                   "identity_evidence": "fixture mapping, not a real game", "status": "InProgress", "live": True, "ended": False,
                   "paused": False, "period": period, "remaining_s": remaining, "clock_semantics": "REMAINING_IN_PERIOD",
                   "clock_evidence": "synthetic fixture contract, not an assertion about sports elapsed",
                   "received_ms": at, "observed_ms": at}
    return f


def events():
    result = []
    for i in range(45):
        at = AT+i*1000
        period, remaining = (2, 600-i) if i < 20 else (4, 210-(i-20)*8)
        frames = [frame(at, asset, period, remaining) for asset in ("a", "b")]
        trades = []
        if i in (5, 8, 12):
            trades = [{"event_id": f"synthetic-print-{i}", "asset_id": "a", "condition_id": "synthetic-condition", "exchange_ms": at,
                       "side": "SELL", "price": ".47", "size": "110" if i == 12 else "3"}]
        result.append({"id": f"synthetic-frame-{i}", "time_ms": at, "frames": frames, "trades": trades, "provenance": "synthetic"})
    return result
