"""Bounded GET-only connectivity probe; no credentials, signing, or orders."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_recorder.api import PublicAPI, GAMMA, utc_ms


async def probe():
    api = PublicAPI()
    try:
        sports = await api.get(GAMMA, "/sports")
        nba = next(s for s in sports if s.get("sport") == "nba")
        events = await api.get(GAMMA, "/events", {"series_id": str(nba["series"]), "closed": "false", "limit": 5})
        games = [e for e in events if str(e.get("slug", "")).startswith("nba-")]
        result = {"checked_ms": utc_ms(), "sports": "connected", "events": "connected", "venue": "polymarket.com", "sample_count": len(events), "nba_series": str(nba["series"]), "order_calls": 0}
        if games:
            detail = await api.get(GAMMA, "/events/slug/"+games[0]["slug"])
            moneyline = next((m for m in detail.get("markets", []) if m.get("sportsMarketType") == "moneyline" and m.get("slug") == detail.get("slug")), None)
            if moneyline:
                ids = moneyline.get("positionIds") if moneyline.get("version") == "v2" else moneyline.get("clobTokenIds")
                assets = json.loads(ids) if isinstance(ids, str) else ids
                book = await api.book(assets[0])
                result.update(book="connected", market_id=moneyline["id"], asset_id=assets[0], tick_size=book.get("tick_size"), minimum_order_size=book.get("min_order_size"))
        return result
    finally:
        await api.close()


if __name__ == "__main__":
    try:
        result = asyncio.run(asyncio.wait_for(probe(), timeout=60))
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__, "order_calls": 0}))
        raise SystemExit(1)
