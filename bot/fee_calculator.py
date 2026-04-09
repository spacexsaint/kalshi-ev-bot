"""
fee_calculator.py — Kalshi fee schedule implementation.

SOURCE: https://kalshi.com/docs/kalshi-fee-schedule.pdf (verified 2026-04-08)

TAKER FEE FORMULA (general markets):
    fees = round_up(0.07 × C × P × (1 − P))
    where:
        P = contract price in dollars (e.g., 0.55 for 55 cents)
        C = number of contracts
        round_up = rounds to the next cent ($0.01)

MAKER FEE FORMULA (general markets):
    fees = round_up(0.0175 × C × P × (1 − P))

SPECIAL CASE — S&P500 (ticker starts "INX") and NASDAQ-100 (ticker starts "NASDAQ100"):
    Taker: fees = round_up(0.035 × C × P × (1 − P))
    Maker: same as general maker = round_up(0.0175 × C × P × (1 − P))

NO fee caps or minimums. Fees only charged on execution (not cancellation).
Rounding: always round UP to the nearest cent.
"""

import math
from typing import Literal


# Fee rate constants — from https://kalshi.com/docs/kalshi-fee-schedule.pdf
_TAKER_RATE_GENERAL: float = 0.07
_MAKER_RATE_GENERAL: float = 0.0175
_TAKER_RATE_INDEX: float = 0.035    # INX / NASDAQ100 markets
_MAKER_RATE_INDEX: float = 0.0175   # same as general maker


def _round_up_cent(value: float) -> float:
    """Round UP to the nearest cent ($0.01). This is Kalshi's fee rounding rule."""
    return math.ceil(value * 100) / 100


def _is_index_market(ticker: str) -> bool:
    """Return True if this market uses the reduced fee schedule (INX / NASDAQ100)."""
    t = ticker.upper()
    return t.startswith("INX") or t.startswith("NASDAQ100")


def compute_taker_fee(
    price_decimal: float,
    num_contracts: float,
    ticker: str = "",
) -> float:
    """
    Compute the total taker fee in USD for a trade.

    Args:
        price_decimal:  Contract price as a decimal in [0, 1].
                        e.g., 55 cents → 0.55
        num_contracts:  Number of contracts (can be fractional for partial fills,
                        but Kalshi issues whole contracts; kept float for precision).
        ticker:         Market ticker string (used to detect index markets).

    Returns:
        Total taker fee in USD, rounded UP to the nearest cent.

    Raises:
        ValueError: if price_decimal is not strictly in (0, 1).
        ValueError: if num_contracts < 0.
    """
    if not (0.0 < price_decimal < 1.0):
        raise ValueError(
            f"price_decimal must be strictly in (0, 1), got {price_decimal}"
        )
    if num_contracts < 0:
        raise ValueError(f"num_contracts must be >= 0, got {num_contracts}")
    if num_contracts == 0:
        return 0.0

    rate = _TAKER_RATE_INDEX if _is_index_market(ticker) else _TAKER_RATE_GENERAL
    raw_fee = rate * num_contracts * price_decimal * (1.0 - price_decimal)
    return _round_up_cent(raw_fee)


def compute_maker_fee(
    price_decimal: float,
    num_contracts: float,
    ticker: str = "",
) -> float:
    """
    Compute the total maker fee in USD for a resting order that gets filled.

    Args:
        price_decimal:  Contract price as a decimal in (0, 1).
        num_contracts:  Number of contracts.
        ticker:         Market ticker (index detection).

    Returns:
        Total maker fee in USD, rounded UP to the nearest cent.
    """
    if not (0.0 < price_decimal < 1.0):
        raise ValueError(
            f"price_decimal must be strictly in (0, 1), got {price_decimal}"
        )
    if num_contracts < 0:
        raise ValueError(f"num_contracts must be >= 0, got {num_contracts}")
    if num_contracts == 0:
        return 0.0

    # Maker rate is the same for general and index markets
    raw_fee = _MAKER_RATE_GENERAL * num_contracts * price_decimal * (1.0 - price_decimal)
    return _round_up_cent(raw_fee)


def compute(
    price_decimal: float,
    num_contracts: float,
    order_type: Literal["taker", "maker"] = "taker",
    ticker: str = "",
) -> float:
    """
    Unified fee computation entry-point (used by edge_calculator).

    Args:
        price_decimal:  Contract price in (0, 1).
        num_contracts:  Number of contracts.
        order_type:     "taker" (default — market orders) or "maker" (limit).
        ticker:         Market ticker for index-market detection.

    Returns:
        Fee in USD.
    """
    if order_type == "taker":
        return compute_taker_fee(price_decimal, num_contracts, ticker)
    return compute_maker_fee(price_decimal, num_contracts, ticker)


def fee_per_contract(
    price_decimal: float,
    ticker: str = "",
    order_type: Literal["taker", "maker"] = "taker",
) -> float:
    """
    Return the fee for exactly 1 contract. Useful for edge calculations
    where you want a per-contract fee estimate before knowing the exact size.
    """
    return compute(price_decimal, 1.0, order_type, ticker)


def max_fee_price() -> float:
    """
    The price at which the fee formula P×(1−P) is maximised — at P=0.50.
    Max taker fee per contract = 0.07 × 1 × 0.5 × 0.5 = $0.0175 → rounds to $0.02.
    """
    return compute_taker_fee(0.50, 1.0)
