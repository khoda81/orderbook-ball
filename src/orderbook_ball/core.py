from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class TopOfBook:
    bid: float
    ask: float

    def __post_init__(self) -> None:
        if not (0 < self.bid <= self.ask):
            raise ValueError(f"invalid top of book: bid={self.bid}, ask={self.ask}")


@dataclass(frozen=True)
class RatioInterval:
    """Executable interval for q = log(A / A')."""

    q_bid: float
    q_ask: float

    @property
    def midpoint(self) -> float:
        return 0.5 * (self.q_bid + self.q_ask)

    @property
    def width(self) -> float:
        return self.q_ask - self.q_bid


def ratio_interval(a: TopOfBook, aprime: TopOfBook) -> RatioInterval:
    """Return the executable log-ratio spread for A/A'.

    Selling A at its bid and buying A' at its ask gives the ratio bid.
    Buying A at its ask and selling A' at its bid gives the ratio ask.

        q_bid = log(a.bid / aprime.ask)
        q_ask = log(a.ask / aprime.bid)
    """
    q_bid = math.log(a.bid) - math.log(aprime.ask)
    q_ask = math.log(a.ask) - math.log(aprime.bid)
    if q_bid > q_ask:
        raise ValueError("crossed ratio interval; inputs are inconsistent")
    return RatioInterval(q_bid, q_ask)


def naive_ratio_of_mids(a: TopOfBook, aprime: TopOfBook) -> float:
    """log(mid(A) / mid(A')); intentionally memoryless baseline."""
    ma = 0.5 * (a.bid + a.ask)
    mb = 0.5 * (aprime.bid + aprime.ask)
    return math.log(ma / mb)


def clip_ball(previous_q: float | None, interval: RatioInterval) -> float:
    """Minimal-motion causal selection from the moving feasible interval."""
    if previous_q is None:
        return interval.midpoint
    return min(max(previous_q, interval.q_bid), interval.q_ask)


def logistic(q: np.ndarray | float) -> np.ndarray | float:
    q_arr = np.asarray(q)
    out = np.empty_like(q_arr, dtype=float)
    pos = q_arr >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-q_arr[pos]))
    eq = np.exp(q_arr[~pos])
    out[~pos] = eq / (1.0 + eq)
    if np.ndim(q) == 0:
        return float(out)
    return out


def temporal_spread_age(
    ts_ms: np.ndarray,
    q_bid: np.ndarray,
    q_ask: np.ndarray,
    q_grid: np.ndarray,
) -> np.ndarray:
    """Age, in seconds, since each q-grid point was last outside the spread.

    Outside points are 0. Points never yet observed outside are NaN. This is a
    discrete representation of the user's 'spread in time' field.
    """
    ts_ms = np.asarray(ts_ms, dtype=np.int64)
    q_bid = np.asarray(q_bid, dtype=float)
    q_ask = np.asarray(q_ask, dtype=float)
    q_grid = np.asarray(q_grid, dtype=float)
    if not (len(ts_ms) == len(q_bid) == len(q_ask)):
        raise ValueError("time and spread arrays must have equal length")
    if len(ts_ms) == 0:
        return np.empty((0, len(q_grid)), dtype=float)

    last_outside = np.full(len(q_grid), np.nan, dtype=float)
    ages = np.full((len(ts_ms), len(q_grid)), np.nan, dtype=float)

    for i, (t, lo, hi) in enumerate(zip(ts_ms, q_bid, q_ask, strict=True)):
        outside = (q_grid < lo) | (q_grid > hi)
        last_outside[outside] = float(t)
        inside = ~outside
        known = inside & np.isfinite(last_outside)
        ages[i, outside] = 0.0
        ages[i, known] = (float(t) - last_outside[known]) / 1000.0

    return ages
