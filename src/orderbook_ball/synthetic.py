from __future__ import annotations

from pathlib import Path
import math

import numpy as np

from .core import TopOfBook
from .io import RatioRecorder


def generate(path: Path, n: int = 1800, seed: int = 7) -> Path:
    # Synthetic generation is deterministic and should replace, not append to, an old run.
    if path.exists():
        path.unlink()
    rng = np.random.default_rng(seed)
    latent_q = np.cumsum(rng.normal(0, 0.018, size=n))
    spread = 0.10 + 0.04 * (1 + np.sin(np.linspace(0, 12, n))) + rng.exponential(0.015, n)
    center_noise = np.convolve(rng.normal(0, 0.02, n), np.ones(8) / 8, mode="same")
    center = latent_q + center_noise
    q_bid = center - spread / 2
    q_ask = center + spread / 2

    # Construct two consistent token books whose executable ratio interval is [q_bid, q_ask].
    # We choose symmetric log-price components around 0.5-ish token prices.
    rec = RatioRecorder(path, "synthetic-binary", "YES", "NO")
    t0 = 1_780_000_000_000
    try:
        for i, (lo, hi) in enumerate(zip(q_bid, q_ask, strict=True)):
            # Solve with a_bid/a'_ask=e^lo and a_ask/a'_bid=e^hi while keeping prices (0,1).
            m = 1.0 / (1.0 + math.exp(-0.5 * (lo + hi)))
            token_spread = min(0.08, 0.012 + 0.002 * spread[i])
            a_bid = max(1e-4, m - token_spread / 2)
            a_ask = min(0.9999, m + token_spread / 2)
            # Derive A' quotes from target ratio interval exactly.
            ap_ask = a_bid / math.exp(lo)
            ap_bid = a_ask / math.exp(hi)
            if not (0 < ap_bid <= ap_ask < 1):
                # Near extremes, use complement-like quotes; still valid for the demo.
                ap_bid = max(1e-4, 1 - a_ask)
                ap_ask = min(0.9999, 1 - a_bid)
            rec.write(t0 + 100 * i, t0 + 100 * i, TopOfBook(a_bid, a_ask), TopOfBook(ap_bid, ap_ask))
    finally:
        rec.close()
    return path
