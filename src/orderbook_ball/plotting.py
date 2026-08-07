from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from .core import logistic, temporal_spread_age


def load_csv(path: Path) -> dict[str, np.ndarray]:
    cols = {k: [] for k in ["ts_ms", "q_bid", "q_ask", "q_mid", "q_ratio_of_mids", "q_ball"]}
    labels = {"market": "", "a_label": "A", "aprime_label": "A'"}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            for k in cols:
                cols[k].append(float(row[k]))
            for k in labels:
                if row.get(k):
                    labels[k] = row[k]
    if not cols["ts_ms"]:
        raise ValueError(f"no rows in {path}")
    out = {k: np.asarray(v) for k, v in cols.items()}
    out.update(labels)
    return out


def plot_recording(
    path: Path,
    out: Path,
    max_points: int = 20_000,
    grid_points: int = 240,
    heatmap_scale: str = "linear",
) -> None:
    d = load_csv(path)
    n = len(d["ts_ms"])
    if n > max_points:
        idx = np.linspace(0, n - 1, max_points).astype(int)
        for k in ["ts_ms", "q_bid", "q_ask", "q_mid", "q_ratio_of_mids", "q_ball"]:
            d[k] = d[k][idx]

    t = (d["ts_ms"] - d["ts_ms"][0]) / 1000.0
    lo = float(np.nanmin(d["q_bid"]))
    hi = float(np.nanmax(d["q_ask"]))
    pad = max(0.05, 0.08 * (hi - lo if hi > lo else 1.0))
    grid = np.linspace(lo - pad, hi + pad, grid_points)
    ages = temporal_spread_age(d["ts_ms"].astype(np.int64), d["q_bid"], d["q_ask"], grid)
    ages_inside = ages.copy()
    inside = (grid[None, :] >= d["q_bid"][:, None]) & (grid[None, :] <= d["q_ask"][:, None])
    ages_inside[~inside] = np.nan

    fig = plt.figure(figsize=(12, 8), constrained_layout=True)
    gs = fig.add_gridspec(2, 1, height_ratios=[2, 1.35])
    ax = fig.add_subplot(gs[0])
    ax.fill_between(t, d["q_bid"], d["q_ask"], alpha=0.2, label="executable log-ratio spread")
    ax.plot(t, d["q_mid"], linewidth=1.0, alpha=0.8, label="ratio-spread midpoint")
    ax.plot(t, d["q_ratio_of_mids"], linewidth=0.9, alpha=0.7, label="log(mid A / mid A')")
    ax.plot(t, d["q_ball"], linewidth=1.7, label="ball / causal clip")
    ax.set_ylabel("q = log(A / A')")
    ax.set_title(f"{d['market']} — {d['a_label']} / {d['aprime_label']}")
    ax.legend(loc="best")
    ax.grid(alpha=0.2)

    ax2 = fig.add_subplot(gs[1], sharex=ax)
    finite = ages_inside[np.isfinite(ages_inside)]
    vmax_seconds = float(np.nanpercentile(finite, 97)) if finite.size else 1.0
    if heatmap_scale == "linear":
        heat_values = ages_inside
        heat_vmax = max(vmax_seconds, 1e-6)
        color_label = "inside-spread age (s)"
    elif heatmap_scale == "log":
        heat_values = np.log1p(ages_inside)
        heat_vmax = max(float(np.log1p(vmax_seconds)), 1e-6)
        color_label = "log(1 + inside-spread age / s)"
    else:
        raise ValueError("heatmap_scale must be 'linear' or 'log'")
    mesh = ax2.pcolormesh(t, grid, heat_values.T, shading="auto", vmin=0, vmax=heat_vmax)
    ax2.plot(t, d["q_ball"], linewidth=1.0, label="ball")
    ax2.set_xlabel("seconds from first event")
    ax2.set_ylabel("q level")
    scale_name = "linear Δt" if heatmap_scale == "linear" else "log(1 + Δt)"
    ax2.set_title(f"Temporal spread: time since each q level was last outside ({scale_name} color)")
    fig.colorbar(mesh, ax=ax2, label=color_label)

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)
