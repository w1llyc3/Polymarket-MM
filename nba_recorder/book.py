from decimal import Decimal


class ResyncRequired(ValueError):
    pass


def number(value, price=False):
    d = Decimal(str(value))
    if not d.is_finite() or d < 0 or (price and d > 1):
        raise ResyncRequired("invalid price/size")
    return d


class Book:
    def __init__(self, market, asset):
        self.market, self.asset = market, asset
        self.bids, self.asks = {}, {}
        self.exchange_ms = None
        self.receive_ms = 0
        self.synced = False
        self.tick = market["tick_size"]
        self.minimum = market["minimum_order_size"]

    def invalidate(self):
        self.synced = False

    def apply(self, event, received):
        timestamp = event.get("timestamp")
        stamp = int(timestamp) if timestamp is not None else None
        if stamp is not None and self.exchange_ms is not None and stamp < self.exchange_ms:
            raise ResyncRequired("out_of_order")
        kind = event["event_type"]
        if kind in ("book", "rest_snapshot"):
            sides = []
            for name in ("bids", "asks"):
                levels = {}
                for x in event[name]:
                    p, s = number(x["price"], True), number(x["size"])
                    if s:
                        levels[p] = s
                sides.append(levels)
            tick = number(event.get("tick_size", self.tick), True)
            minimum = number(event.get("min_order_size", self.minimum))
            if tick <= 0 or minimum <= 0:
                raise ResyncRequired("invalid tick/minimum size")
            self.bids, self.asks = sides
            self.synced = True
            self.tick, self.minimum = str(tick), str(minimum)
        elif kind == "price_change":
            if not self.synced:
                raise ResyncRequired("delta_before_snapshot")
            # Atomic batch; sizes are absolute quantities, zero removes a level.
            bids, asks = self.bids.copy(), self.asks.copy()
            for change in event["price_changes"]:
                if str(change["asset_id"]) != self.asset:
                    continue
                if change["side"] not in ("BUY", "SELL"):
                    raise ResyncRequired("invalid_side")
                side = bids if change["side"] == "BUY" else asks
                p, s = number(change["price"], True), number(change["size"])
                if s:
                    side[p] = s
                else:
                    side.pop(p, None)
            self.bids, self.asks = bids, asks
            # Public feed has no documented sequence/replay guarantee.
            self.synced = False
        elif kind == "tick_size_change":
            tick = number(event["new_tick_size"], True)
            if tick <= 0:
                raise ResyncRequired("invalid tick")
            self.tick = str(tick)
        else:
            return False
        self.exchange_ms, self.receive_ms = stamp, received
        return True

    def snapshot(self, now, stale_seconds=30):
        bids = [{"price": str(p), "size": str(s)} for p, s in sorted(self.bids.items(), reverse=True)]
        asks = [{"price": str(p), "size": str(s)} for p, s in sorted(self.asks.items())]
        bid, ask = (max(self.bids) if self.bids else None), (min(self.asks) if self.asks else None)
        flags = []
        if bid is None or ask is None:
            flags.append("empty")
        elif bid > ask:
            flags.append("crossed")
        elif bid == ask:
            flags.append("locked")
        if not self.synced:
            flags.append("unverified_continuity")
        if now - self.receive_ms > stale_seconds * 1000:
            flags.append("stale")
        return {"venue": "polymarket.com", "market_id": self.market["market_id"], "condition_id": self.market["condition_id"],
                "asset_id": self.asset, "exchange_timestamp": self.exchange_ms, "local_receive_timestamp": self.receive_ms,
                "bid": str(bid) if bid is not None else None, "ask": str(ask) if ask is not None else None,
                "spread": str(ask - bid) if bid is not None and ask is not None else None,
                "bids": bids, "asks": asks, "flags": flags, "tick_size": self.tick, "minimum_order_size": self.minimum}
