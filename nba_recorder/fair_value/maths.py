from decimal import Decimal, InvalidOperation, localcontext

PRECISION = 42


def decimal(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError("invalid numeric value") from None
    if not result.is_finite():
        raise ValueError("non-finite value")
    return result


def implied_probability(odds, odds_format):
    value = decimal(odds)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        if odds_format == "decimal":
            if value <= 1:
                raise ValueError("decimal odds must exceed 1")
            return 1 / value
        if odds_format == "american":
            if abs(value) < 100 or value != value.to_integral_value():
                raise ValueError("American odds require an integer <=-100 or >=100")
            return 100 / (value + 100) if value > 0 else -value / (100 - value)
        raise ValueError("unsupported odds format")


def proportional_devig(home_odds, away_odds, odds_format):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        home = implied_probability(home_odds, odds_format)
        away = implied_probability(away_odds, odds_format)
        total = home + away
        fair_home = home / total
        return {"home": str(fair_home), "away": str(1 - fair_home),
                "implied_home": str(home), "implied_away": str(away), "overround": str(total - 1)}


def consensus(probabilities):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        prices = sorted(decimal(p) for p in probabilities)
        if not prices or any(not 0 < p < 1 for p in prices):
            raise ValueError("consensus requires valid probabilities")
        mean = sum(prices) / len(prices)
        stddev = (sum((p - mean) ** 2 for p in prices) / len(prices)).sqrt()
        return {"home": str(mean), "away": str(1 - mean), "stddev": str(stddev), "range": str(prices[-1] - prices[0])}
