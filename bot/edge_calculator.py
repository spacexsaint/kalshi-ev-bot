"""
edge_calculator.py — EV, Kelly, and fee-adjusted edge logic.

KELLY CRITERION FOR BINARY PREDICTION MARKETS:
  Kalshi contracts pay exactly $1 on resolution.

  For YES at price p (decimal, e.g. 0.55):
    f_yes = (w - p) / (1 - p)

  For NO at price q (decimal, e.g. 0.48):
    f_no  = ((1 - w) - q) / (1 - q)

  where w = fair probability from external sources (0–1 decimal)
  f represents the fraction of bankroll to wager.

EV CALCULATION (net of fees):
  gross_ev_yes = w × (1 - p) - (1 - w) × p
  fee          = fee_calculator.compute(p, contracts)
  net_ev_yes   = gross_ev_yes - fee (per contract)
  net_edge     = net_ev_yes / p

  Proceed only if net_edge >= MIN_EDGE (5%).

STAKE SIZING:
  stake = min(KELLY_FRACTION × f × balance, MAX_BET_PCT × balance)
  stake = max(stake, MIN_BET_USD)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

from bot import config
from bot import fee_calculator


@dataclass
class EdgeResult:
    direction: Literal["YES", "NO", "NONE"]
    gross_edge: float        # Gross edge (before fees), fraction of stake
    net_edge: float          # Net edge (after fees), fraction of stake
    kelly_fraction: float    # Raw Kelly fraction f
    stake_usd: float         # Dollar amount to bet (sized by Kelly + caps)
    fair_prob: float         # w — external fair probability
    market_price: float      # p (YES side) or q (NO side), decimal
    fee_usd: float           # Estimated fee for the computed stake
    gross_ev: float          # Gross expected value in dollars
    net_ev: float            # Net expected value in dollars


def _validate_probability(value: float, name: str) -> None:
    if not (0.0 < value < 1.0):
        raise ValueError(
            f"{name} must be strictly in (0, 1). Got: {value}"
        )


def _gross_ev_yes(w: float, p: float) -> float:
    """
    Gross expected value per $1 staked on YES at price p.
    A YES contract pays $1 if the event occurs (prob w), $0 otherwise.
    Cost to enter = p.
    Gross EV = w*(1−p) − (1−w)*p
    """
    return w * (1.0 - p) - (1.0 - w) * p


def _gross_ev_no(w: float, q: float) -> float:
    """
    Gross expected value per $1 staked on NO at price q.
    A NO contract pays $1 if the event does NOT occur (prob 1−w), $0 otherwise.
    Gross EV = (1−w)*(1−q) − w*q
    """
    return (1.0 - w) * (1.0 - q) - w * q


def _kelly_yes(w: float, p: float) -> float:
    """Kelly fraction for a YES position."""
    return (w - p) / (1.0 - p)


def _kelly_no(w: float, q: float) -> float:
    """Kelly fraction for a NO position."""
    return ((1.0 - w) - q) / (1.0 - q)


def _size_stake(kelly_f: float, balance: float) -> float:
    """Apply Quarter-Kelly and position limits to produce dollar stake."""
    raw = config.KELLY_FRACTION * kelly_f * balance
    capped = min(raw, config.MAX_BET_PCT * balance)
    return max(capped, config.MIN_BET_USD)


def _net_edge_yes(
    w: float,
    p: float,
    balance: float,
    ticker: str = "",
) -> tuple[float, float, float, float, float, float]:
    """
    Compute net edge for a YES bet.

    Returns:
        (gross_edge, net_edge, kelly_f, stake_usd, fee_usd, net_ev)
    """
    kelly_f = _kelly_yes(w, p)
    if kelly_f <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    stake = _size_stake(kelly_f, balance)
    num_contracts = math.floor(stake / p)
    if num_contracts < 1:
        num_contracts = 1

    fee = fee_calculator.compute_taker_fee(p, num_contracts, ticker)
    gross_ev_per_contract = _gross_ev_yes(w, p)
    total_gross_ev = gross_ev_per_contract * num_contracts
    total_net_ev = total_gross_ev - fee

    gross_edge = gross_ev_per_contract / p
    net_edge = total_net_ev / (num_contracts * p) if num_contracts > 0 else 0.0

    return gross_edge, net_edge, kelly_f, stake, fee, total_net_ev


def _net_edge_no(
    w: float,
    q: float,
    balance: float,
    ticker: str = "",
) -> tuple[float, float, float, float, float, float]:
    """
    Compute net edge for a NO bet.

    Returns:
        (gross_edge, net_edge, kelly_f, stake_usd, fee_usd, net_ev)
    """
    kelly_f = _kelly_no(w, q)
    if kelly_f <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    stake = _size_stake(kelly_f, balance)
    num_contracts = math.floor(stake / q)
    if num_contracts < 1:
        num_contracts = 1

    fee = fee_calculator.compute_taker_fee(q, num_contracts, ticker)
    gross_ev_per_contract = _gross_ev_no(w, q)
    total_gross_ev = gross_ev_per_contract * num_contracts
    total_net_ev = total_gross_ev - fee

    gross_edge = gross_ev_per_contract / q
    net_edge = total_net_ev / (num_contracts * q) if num_contracts > 0 else 0.0

    return gross_edge, net_edge, kelly_f, stake, fee, total_net_ev


def compute_edge(
    w: float,          # Fair probability from external sources
    p: float,          # YES ask price (decimal)
    q: float,          # NO ask price (decimal)
    balance: float,    # Current account balance in USD
    ticker: str = "",  # For index-market fee detection
) -> EdgeResult:
    """
    Compute the optimal betting direction and size using Kelly criterion.

    Args:
        w:       Fair probability (0–1) from Manifold/PredictIt aggregation.
        p:       Kalshi YES ask price as decimal (e.g., 0.55 for 55 cents).
        q:       Kalshi NO ask price as decimal (e.g., 0.48 for 48 cents).
        balance: Current USD balance for sizing.
        ticker:  Market ticker (for INX/NASDAQ100 fee discount).

    Returns:
        EdgeResult with direction="YES"|"NO"|"NONE" and all computed values.

    Raises:
        ValueError: if any probability is outside (0, 1).
    """
    _validate_probability(w, "fair_prob (w)")
    _validate_probability(p, "yes_price (p)")
    _validate_probability(q, "no_price (q)")

    if balance <= 0:
        return EdgeResult(
            direction="NONE",
            gross_edge=0.0,
            net_edge=0.0,
            kelly_fraction=0.0,
            stake_usd=0.0,
            fair_prob=w,
            market_price=p,
            fee_usd=0.0,
            gross_ev=0.0,
            net_ev=0.0,
        )

    # Evaluate YES
    yes_gross, yes_net, yes_kelly, yes_stake, yes_fee, yes_ev = _net_edge_yes(
        w, p, balance, ticker
    )
    yes_qualifies = yes_net >= config.MIN_EDGE

    # Evaluate NO
    no_gross, no_net, no_kelly, no_stake, no_fee, no_ev = _net_edge_no(
        w, q, balance, ticker
    )
    no_qualifies = no_net >= config.MIN_EDGE

    # No edge on either side
    if not yes_qualifies and not no_qualifies:
        best_gross = max(yes_gross, no_gross, 0.0)
        best_net = max(yes_net, no_net, 0.0)
        return EdgeResult(
            direction="NONE",
            gross_edge=best_gross,
            net_edge=best_net,
            kelly_fraction=0.0,
            stake_usd=0.0,
            fair_prob=w,
            market_price=p,
            fee_usd=0.0,
            gross_ev=0.0,
            net_ev=0.0,
        )

    # Both qualify → pick larger net edge
    if yes_qualifies and no_qualifies:
        if yes_net >= no_net:
            return EdgeResult(
                direction="YES",
                gross_edge=yes_gross,
                net_edge=yes_net,
                kelly_fraction=yes_kelly,
                stake_usd=yes_stake,
                fair_prob=w,
                market_price=p,
                fee_usd=yes_fee,
                gross_ev=yes_ev,
                net_ev=yes_ev,
            )
        else:
            return EdgeResult(
                direction="NO",
                gross_edge=no_gross,
                net_edge=no_net,
                kelly_fraction=no_kelly,
                stake_usd=no_stake,
                fair_prob=w,
                market_price=q,
                fee_usd=no_fee,
                gross_ev=no_ev,
                net_ev=no_ev,
            )

    if yes_qualifies:
        return EdgeResult(
            direction="YES",
            gross_edge=yes_gross,
            net_edge=yes_net,
            kelly_fraction=yes_kelly,
            stake_usd=yes_stake,
            fair_prob=w,
            market_price=p,
            fee_usd=yes_fee,
            gross_ev=yes_ev,
            net_ev=yes_ev,
        )

    return EdgeResult(
        direction="NO",
        gross_edge=no_gross,
        net_edge=no_net,
        kelly_fraction=no_kelly,
        stake_usd=no_stake,
        fair_prob=w,
        market_price=q,
        fee_usd=no_fee,
        gross_ev=no_ev,
        net_ev=no_ev,
    )
