from __future__ import annotations

from collections import Counter
from pathlib import Path

from huggingface_hub import hf_hub_download
import pyarrow.parquet as pq

from .core import TopOfBook
from .io import RatioRecorder

REPO = "gregyoung14/openmarket-btc-polymarket"


def make_demo_csv(out: Path, market_slug: str | None = None) -> Path:
    tick_file = Path(hf_hub_download(REPO, "polymarket_ticks_ms.parquet", repo_type="dataset"))
    meta_file = Path(hf_hub_download(REPO, "market_meta.parquet", repo_type="dataset"))

    ticks = pq.read_table(tick_file).to_pylist()
    meta = pq.read_table(meta_file).to_pylist()

    if market_slug is None:
        counts = Counter(r["market_slug"] for r in ticks)
        market_slug = counts.most_common(1)[0][0]

    meta_by_slug = {r["slug"]: r for r in meta}
    m = meta_by_slug.get(market_slug, {})
    a_token = str(m.get("up_token_id") or "")
    aprime_token = str(m.get("down_token_id") or "")

    rows = sorted((r for r in ticks if r["market_slug"] == market_slug), key=lambda r: r["source_ts_ms"])
    if not rows:
        raise RuntimeError(f"market {market_slug!r} not present in sample")

    # Fall back to labels if metadata is missing.
    labels = sorted({str(r.get("side_label")) for r in rows})
    if not a_token or not aprime_token:
        if len(labels) != 2:
            raise RuntimeError("could not identify both opposing tokens")
        a_label, aprime_label = labels[0], labels[1]
    else:
        a_label, aprime_label = "up", "down"

    books: dict[str, TopOfBook] = {}
    rec = RatioRecorder(out, market_slug, a_label, aprime_label)
    try:
        for r in rows:
            bid = r.get("best_bid")
            ask = r.get("best_ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
                continue
            token = str(r.get("asset_id") or "")
            label = str(r.get("side_label") or "")
            key = token if token else label
            books[key] = TopOfBook(float(bid), float(ask))

            if a_token and aprime_token:
                ka, kb = a_token, aprime_token
            else:
                ka, kb = a_label, aprime_label
            if ka in books and kb in books:
                ts = int(r["source_ts_ms"])
                recv = int(r.get("ingest_ts_ms") or ts)
                rec.write(ts, recv, books[ka], books[kb])
    finally:
        rec.close()
    return out
