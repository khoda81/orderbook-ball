from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .core import TopOfBook, clip_ball, naive_ratio_of_mids, ratio_interval


FIELDNAMES = [
    "ts_ms",
    "recv_ts_ms",
    "market",
    "a_label",
    "aprime_label",
    "a_bid",
    "a_ask",
    "aprime_bid",
    "aprime_ask",
    "q_bid",
    "q_ask",
    "q_mid",
    "q_ratio_of_mids",
    "q_ball",
]


@dataclass
class RatioRecorder:
    path: Path
    market: str
    a_label: str
    aprime_label: str
    _ball: float | None = None

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = self.path.open("a", newline="", buffering=1)
        self._writer = csv.DictWriter(self._fh, fieldnames=FIELDNAMES)
        if is_new:
            self._writer.writeheader()

    def close(self) -> None:
        self._fh.close()

    def write(self, ts_ms: int, recv_ts_ms: int, a: TopOfBook, aprime: TopOfBook) -> dict[str, object]:
        interval = ratio_interval(a, aprime)
        self._ball = clip_ball(self._ball, interval)
        row = {
            "ts_ms": int(ts_ms),
            "recv_ts_ms": int(recv_ts_ms),
            "market": self.market,
            "a_label": self.a_label,
            "aprime_label": self.aprime_label,
            "a_bid": a.bid,
            "a_ask": a.ask,
            "aprime_bid": aprime.bid,
            "aprime_ask": aprime.ask,
            "q_bid": interval.q_bid,
            "q_ask": interval.q_ask,
            "q_mid": interval.midpoint,
            "q_ratio_of_mids": naive_ratio_of_mids(a, aprime),
            "q_ball": self._ball,
        }
        self._writer.writerow(row)
        return row
