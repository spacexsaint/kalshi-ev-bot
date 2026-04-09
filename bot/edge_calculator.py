"""
edge_calculator.py — EV, Kelly, fee-aware position sizing.

AUDIT LOG (chronological):
─────────────────────────────────────────────────────────────────────
v1 (original): Kelly used ask; EV used ask — coherent but ask-biased.

v2 (2026-04-08): Kelly used MIDPOINT; EV used ASK — INCOHERENT BUG.
  Wide spread (bid=0.50, ask=0.65, mid=0.575, w=0.60):
  Kelly(mid=0.575) > 0, EV(ask=0.65) < 0 → contradictory signal.

v3 (2026-04-09, CURRENT): Kelly uses ASK; EV uses ASK — coherent.
  Midpoint stored as market_price (display/logging only, never in math).
  Fee circular dependency fixed: estimate fee before contract count, then
  deduct from available budget so total_cost ≤ intended stake.

MATHEMATICAL BASIS:
  Binary Kalshi contract: pay p (ask), receive $1 if YES (w prob), $0 if NO.
  Optimal Kelly fraction (Meister 2024, arXiv:2412.14144):
    f_yes = (w - p) / (1 - p)    [derivative of E[log wealth] = 0]
    f_no  = ((1-w) - q) / (1 - q)
  Both Kelly and EV MUST use the same price (exec_price = ask).

FEE CORRECTION:
  Old:  contracts = floor(stake / p); fee added on top → overrun up to ~$1
  New:  contracts = floor((stake - fee_estimate) / p)
  This ensures total_cost = contracts*p + fee ≤ stake (within 1 cent).

ADJUSTMENTS APPLIED (all multiplicative to Kelly fraction):
  1. Quarter-Kelly   (KELLY_FRACTION = 0.25)
  2. KL-uncertainty  (0.50 / 0.75 / 1.00 for 1/2/3+ sources)
  3. Time-decay      (linear 1.0→0.60 from 24h to 2h before close)
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
    gross_edge: float        # Fraction of stake before fees
    net_edge: float          # Fraction of stake after fees
    kelly_fraction: float    # Raw Kelly f (at exec price, before multipliers)
    adjusted_kelly: float    # After KL-uncertainty + time-decay
    stake_usd: float         # Intended budget (contracts*p + fee ≤ this)
    contracts: int           # Whole contracts to buy
    fair_prob: float         # w from external aggregation
    market_price: float      # Midpoint (display only — NOT used in any math)
    exec_price: float        # Ask price — used for ALL calculations
    fee_usd: float           # Taker fee for this trade
    gross_ev: float          # Total gross EV in dollars
    net_ev: float            # Total net EV in dollars (after fee)
    time_decay_mult: float
    uncertainty_mult: float
    source_count: int
    min_edge_used: float = 0.05  # Adaptive threshold that was actually applied


def _validate_probability(value: float, name: str) -> None:
    if not (0.0 < value < 1.0):
        raise ValueError(f"{name} must be strictly in (0, 1). Got: {value}")


def compute_time_decay_multiplier(hours_to_close: Optional[float]) -> float:
    """Linear decay from 1.0 (at threshold) to MIN_MULTIPLIER (at floor)."""
    if hours_to_close is None or hours_to_close >= config.TIME_DECAY_THRESHOLD_HR:
        return 1.0
    floor = float(config.MIN_TIME_TO_CLOSE_HR)
    thr = config.TIME_DECAY_THRESHOLD_HR
    mn = config.TIME_DECAY_MIN_MULTIPLIER
    t = max(floor, min(hours_to_close, thr))
    return mn + (t - floor) / max(thr - floor, 0.001) * (1.0 - mn)


def compute_uncertainty_multiplier(source_count: int) -> float:
    """KL-divergence uncertainty penalty (Meister 2024, Galekwa 2026)."""
    if source_count >= 3:
        return config.KL_UNCERTAINTY_PENALTY_TRIPLE_SOURCE
    if source_count == 2:
        return config.KL_UNCERTAINTY_PENALTY_DUAL_SOURCE
    return config.KL_UNCERTAINTY_PENALTY_SINGLE_SOURCE


def get_min_edge(source_count: int) -> float:
    """
    Adaptive minimum edge threshold by source confidence level.

    Rationale: A triple-source consensus (PredictIt + Manifold + Polymarket
    all agreeing) gives a much more reliable w estimate than a single source.
    Requiring the same 5% edge for both ignores this signal quality difference.

    Combined with the KL-uncertainty stake multiplier, this creates a two-layer
    filter: we both reduce stake size AND require a larger edge for uncertain bets.

    Thresholds (config.py):
      Triple: 3%  — high confidence, lower bar acceptable
      Dual:   5%  — standard bar
      Single: 8%  — extra margin to compensate for w estimation uncertainty
    """
    if source_count >= 3:
        return config.MIN_EDGE_TRIPLE_SOURCE
    if source_count == 2:
        return config.MIN_EDGE_DUAL_SOURCE
    return config.MIN_EDGE_SINGLE_SOURCE


def _compute_midpoint(bid: float, ask: float) -> float:
    """Display/logging only — never used in financial calculations."""
    return (bid + ask) / 2.0 if bid > 0 else ask


def _gross_ev_yes(w: float, p: float) -> float:
    """EV per contract for YES at price p. Equals (w - p)."""
    return w * (1.0 - p) - (1.0 - w) * p


def _gross_ev_no(w: float, q: float) -> float:
    """EV per contract for NO at price q. Equals ((1-w) - q)."""
    return (1.0 - w) * (1.0 - q) - w * q


def _kelly_yes(w: float, p: float) -> float:
    """f_yes = (w - p)/(1 - p). Meister 2024 arXiv:2412.14144."""
    return (w - p) / (1.0 - p)


def _kelly_no(w: float, q: float) -> float:
    """f_no = ((1-w) - q)/(1 - q). Meister 2024 arXiv:2412.14144."""
    return ((1.0 - w) - q) / (1.0 - q)


def _solve_contracts_with_fee(
    stake: float,
    price: float,
    ticker: str,
    max_iter: int = 5,
) -> tuple[int, float]:
    """
    Solve for the largest contract count where contracts*price + fee ≤ stake.

    Uses iterative refinement to handle the fee circular dependency:
      1. Check if even 1 contract is affordable (early exit if not).
      2. Estimate fee for floor(stake/price) contracts.
      3. Reduce budget by fee, recompute contracts.
      4. Repeat until stable (converges in 2-3 iterations).

    Returns:
        (num_contracts, actual_fee)
    """
    if price <= 0 or stake <= 0:
        return 0, 0.0

    # BUG FIX: Always check whether even 1 contract is affordable before proceeding.
    # Without this check, the function returns n=1 even when 1*price + fee > stake,
    # causing the caller to commit more than the intended budget.
    fee_1 = fee_calculator.compute_taker_fee(price, 1, ticker)
    if price + fee_1 > stake + 0.005:   # 0.5c tolerance for float rounding
        return 0, 0.0

    n = max(1, math.floor(stake / price))
    for _ in range(max_iter):
        fee = fee_calculator.compute_taker_fee(price, n, ticker)
        budget_after_fee = stake - fee
        if budget_after_fee <= 0:
            return 1, fee_1   # Already verified 1 contract is affordable above
        n_new = max(1, math.floor(budget_after_fee / price))
        if n_new == n:
            break
        n = n_new

    fee = fee_calculator.compute_taker_fee(price, n, ticker)
    return n, fee


def _compute_side(
    w: float,
    bid: float,
    ask: float,
    balance: float,
    uncertainty_mult: float,
    time_decay_mult: float,
    ticker: str,
    is_yes: bool,
) -> tuple[float, float, float, float, int, float, float, float, float]:
    """
    Compute all metrics for one side (YES or NO).

    Both Kelly fraction and EV use exec_price (ask) — coherent pricing.

    Returns:
        (gross_edge, net_edge, kelly_f, adjusted_kelly, contracts,
         fee_usd, net_ev, exec_price, midpoint)
    """
    exec_price = ask
    midpoint = _compute_midpoint(bid, ask)  # Display only

    # Kelly at exec price (same price used for EV)
    kelly_f = _kelly_yes(w, exec_price) if is_yes else _kelly_no(w, exec_price)
    if kelly_f <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, exec_price, midpoint

    adjusted_kelly = kelly_f * uncertainty_mult * time_decay_mult
    raw_stake = config.KELLY_FRACTION * adjusted_kelly * balance
    stake = min(raw_stake, config.MAX_BET_PCT * balance)
    # BUG FIX: MIN_BET_USD floor must NEVER exceed actual balance.
    # Without this guard, a $0.01 balance with MIN_BET_USD=$1.00 produces a
    # 10,000% overbet. Only apply the floor if balance can support it.
    if balance >= config.MIN_BET_USD:
        stake = max(stake, config.MIN_BET_USD)
    # else: leave stake unclamped — _solve_contracts will return 0 if unaffordable

    # Fee-aware contract sizing (fixes circular dependency)
    contracts, fee = _solve_contracts_with_fee(stake, exec_price, ticker)
    if contracts < 1:
        return 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, exec_price, midpoint

    # EV at exec price (coherent with Kelly)
    gev_per = _gross_ev_yes(w, exec_price) if is_yes else _gross_ev_no(w, exec_price)
    total_gross_ev = gev_per * contracts
    total_net_ev = total_gross_ev - fee

    gross_edge = gev_per / exec_price
    net_edge = total_net_ev / (contracts * exec_price)

    return gross_edge, net_edge, kelly_f, adjusted_kelly, contracts, fee, total_net_ev, exec_price, midpoint


def compute_edge(
    w: float,
    p: float,
    q: float,
    balance: float,
    ticker: str = "",
    yes_bid: float = 0.0,
    no_bid: float = 0.0,
    hours_to_close: Optional[float] = None,
    source_count: int = 1,
) -> EdgeResult:
    """
    Compute optimal bet direction and size.

    Pricing: ALL math (Kelly + EV) uses exec_price = ask.
    Midpoint stored in market_price for display only.

    Args:
        w:              Fair probability (0–1).
        p:              YES ask price (decimal, 0–1).
        q:              NO ask price (decimal, 0–1).
        balance:        Current USD balance.
        ticker:         Market ticker (INX/NASDAQ100 fee detection).
        yes_bid:        YES bid (midpoint display only).
        no_bid:         NO bid (midpoint display only).
        hours_to_close: For time-decay calculation.
        source_count:   Number of sources matched (for KL penalty).
    """
    _validate_probability(w, "fair_prob (w)")
    _validate_probability(p, "yes_price (p)")
    _validate_probability(q, "no_price (q)")

    td = compute_time_decay_multiplier(hours_to_close)
    um = compute_uncertainty_multiplier(source_count)
    min_edge = get_min_edge(source_count)  # Adaptive: 3%/5%/8% by confidence

    def _no_bet(exec_p: float = p) -> EdgeResult:
        return EdgeResult(
            direction="NONE", gross_edge=0.0, net_edge=0.0,
            kelly_fraction=0.0, adjusted_kelly=0.0, stake_usd=0.0,
            contracts=0, fair_prob=w,
            market_price=_compute_midpoint(yes_bid, p) if yes_bid > 0 else p,
            exec_price=exec_p, fee_usd=0.0, gross_ev=0.0, net_ev=0.0,
            time_decay_mult=td, uncertainty_mult=um, source_count=source_count,
            min_edge_used=min_edge,
        )

    if balance <= 0:
        return _no_bet()

    # YES side
    y = _compute_side(w, yes_bid, p, balance, um, td, ticker, is_yes=True)
    yg, yn, yk, ya, yc, yf, yev, yp, ymid = y
    yes_ok = yn >= min_edge and yc >= 1

    # NO side
    n_ = _compute_side(w, no_bid, q, balance, um, td, ticker, is_yes=False)
    ng, nn, nk, na, nc, nf, nev, np_, nmid = n_
    no_ok = nn >= min_edge and nc >= 1

    if not yes_ok and not no_ok:
        return _no_bet()

    def _yes_result() -> EdgeResult:
        stake = yc * yp + yf
        return EdgeResult(
            direction="YES", gross_edge=yg, net_edge=yn,
            kelly_fraction=yk, adjusted_kelly=ya, stake_usd=stake,
            contracts=yc, fair_prob=w, market_price=ymid, exec_price=yp,
            fee_usd=yf, gross_ev=yg * yc * yp, net_ev=yev,
            time_decay_mult=td, uncertainty_mult=um, source_count=source_count,
            min_edge_used=min_edge,
        )

    def _no_result() -> EdgeResult:
        stake = nc * np_ + nf
        return EdgeResult(
            direction="NO", gross_edge=ng, net_edge=nn,
            kelly_fraction=nk, adjusted_kelly=na, stake_usd=stake,
            contracts=nc, fair_prob=w, market_price=nmid, exec_price=np_,
            fee_usd=nf, gross_ev=ng * nc * np_, net_ev=nev,
            time_decay_mult=td, uncertainty_mult=um, source_count=source_count,
            min_edge_used=min_edge,
        )

    if yes_ok and no_ok:
        return _yes_result() if yn >= nn else _no_result()
    return _yes_result() if yes_ok else _no_result()
