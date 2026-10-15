from dataclasses import dataclass

from ..fair_value.maths import decimal


@dataclass(frozen=True)
class ModelConfig:
    queue_multiplier: str = "1"
    hidden_ahead: str = "0"
    maker_fee_rate: str = "0"
    taker_fee_rate: str | None = None
    fee_source: str = "research_assumption_not_market_verified"

    def __post_init__(self):
        if decimal(self.queue_multiplier) < 0 or decimal(self.hidden_ahead) < 0:
            raise ValueError("queue assumptions must be nonnegative")
        for rate in (self.maker_fee_rate, self.taker_fee_rate):
            if rate is not None and not 0 <= decimal(rate) < 1:
                raise ValueError("invalid fee assumption")


ASSUMPTIONS = {
    "L0": {"name": "TOUCH", "confidence": "low_uncalibrated", "assumptions": ["opposing BBO touch stands in for a fill", "observed top depth caps size", "no queue priority or true fill probability"]},
    "L1": {"name": "TRADE_THROUGH", "confidence": "low_uncalibrated", "assumptions": ["strict trade-through only", "public side treated as aggressor without independent proof", "public volume caps fills", "no queue position"]},
    "L2": {"name": "QUEUE_AWARE", "confidence": "very_low_uncalibrated", "assumptions": ["FIFO and price priority assumed", "queue ahead estimated from better-price and same-price visible depth at order acceptance", "configured hidden volume may be ahead", "only eligible prints advance queue", "cancellations/depth disappearance do not advance queue", "public side treated as aggressor", "not real queue position or fill probability"]},
}


def queue_ahead(order, frame, config):
    level = "bids" if order["side"] == "BUY" else "asks"
    price = decimal(order["price"])
    size = sum((decimal(x["size"]) for x in frame["book"][level] if (decimal(x["price"]) >= price if order["side"] == "BUY" else decimal(x["price"]) <= price)), decimal(0))
    if size < 0:
        raise ValueError("negative queue depth")
    return size*decimal(config.queue_multiplier)+decimal(config.hidden_ahead)


def order_quantities(order):
    submitted, unfilled = decimal(order["size"]), decimal(order["remaining"])
    filled = submitted-unfilled
    cancelled = unfilled if order["state"] == "CANCELLED" else decimal(0)
    expired = unfilled if order["state"] == "EXPIRED" else decimal(0)
    working = unfilled if order["state"] not in {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"} else decimal(0)
    if min(submitted, unfilled, filled) < 0 or submitted != filled+working+cancelled+expired:
        raise ValueError("virtual order quantity conservation failure")
    return {"submitted_quantity": str(submitted), "filled_quantity": str(filled), "unfilled_quantity": str(unfilled),
            "working_quantity": str(working), "cancelled_quantity": str(cancelled), "expired_quantity": str(expired)}
