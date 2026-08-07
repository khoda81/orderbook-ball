from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
import time
from urllib.parse import urlparse

import httpx
import websockets

from .core import TopOfBook
from .io import RatioRecorder

GAMMA = "https://gamma-api.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(frozen=True)
class BinaryMarket:
    slug: str
    question: str
    condition_id: str
    a_label: str
    aprime_label: str
    a_token: str
    aprime_token: str


def _decode_jsonish(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _slug_from_input(value: str) -> str:
    if "://" in value:
        path = urlparse(value).path.rstrip("/")
        return path.split("/")[-1]
    return value.strip().rstrip("/").split("/")[-1]


def _market_from_gamma(m: dict) -> BinaryMarket | None:
    outcomes = _decode_jsonish(m.get("outcomes"))
    token_ids = _decode_jsonish(m.get("clobTokenIds") or m.get("clob_token_ids"))
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return None
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None
    return BinaryMarket(
        slug=str(m.get("slug") or "market"),
        question=str(m.get("question") or m.get("title") or ""),
        condition_id=str(m.get("conditionId") or m.get("condition_id") or ""),
        a_label=str(outcomes[0]),
        aprime_label=str(outcomes[1]),
        a_token=str(token_ids[0]),
        aprime_token=str(token_ids[1]),
    )


async def resolve_binary_markets(value: str) -> list[BinaryMarket]:
    """Resolve a Polymarket market/event URL or slug into binary CLOB markets."""
    slug = _slug_from_input(value)
    async with httpx.AsyncClient(timeout=15) as client:
        # A direct market slug is the cheapest and most precise lookup.
        r = await client.get(f"{GAMMA}/markets", params={"slug": slug})
        r.raise_for_status()
        markets = r.json()
        if isinstance(markets, list):
            direct = [x for x in (_market_from_gamma(m) for m in markets) if x]
            if direct:
                return direct

        # Event URLs may contain one or several binary submarkets.
        r = await client.get(f"{GAMMA}/events/slug/{slug}")
        r.raise_for_status()
        event = r.json()
        candidates = [x for x in (_market_from_gamma(m) for m in event.get("markets", [])) if x]
        if not candidates:
            raise RuntimeError(f"no binary CLOB market found for {value!r}")
        return candidates


async def resolve_binary_market(value: str, market_index: int | None = None) -> BinaryMarket:
    candidates = await resolve_binary_markets(value)
    if len(candidates) == 1:
        return candidates[0]
    if market_index is None:
        choices = "\n".join(f"  [{i}] {m.question}" for i, m in enumerate(candidates))
        raise RuntimeError(
            "event contains multiple binary markets; rerun with --market-index N:\n" + choices
        )
    if market_index < 0 or market_index >= len(candidates):
        raise RuntimeError(f"--market-index must be in [0, {len(candidates)-1}]")
    return candidates[market_index]


def _best_from_book(levels: list[dict], side: str) -> float | None:
    vals = [float(x["price"]) for x in levels if float(x.get("price", 0)) > 0]
    if not vals:
        return None
    return max(vals) if side == "bid" else min(vals)


def _message_updates(msg: dict) -> list[tuple[str, float | None, float | None, int | None]]:
    """Normalize raw Polymarket WS formats into (token, bid, ask, source_ts_ms)."""
    event_type = msg.get("event_type") or msg.get("type")
    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else msg
    ts = payload.get("timestamp") or msg.get("timestamp")
    try:
        ts_ms = int(ts) if ts is not None and str(ts).isdigit() else None
    except (TypeError, ValueError):
        ts_ms = None

    if event_type == "best_bid_ask":
        token = payload.get("asset_id") or payload.get("token_id") or payload.get("tokenId")
        bid = payload.get("best_bid") or payload.get("bestBid")
        ask = payload.get("best_ask") or payload.get("bestAsk")
        return [(str(token), float(bid) if bid else None, float(ask) if ask else None, ts_ms)] if token else []

    if event_type == "book":
        token = payload.get("asset_id") or payload.get("token_id") or payload.get("tokenId")
        if not token:
            return []
        bid = _best_from_book(payload.get("bids", []), "bid")
        ask = _best_from_book(payload.get("asks", []), "ask")
        return [(str(token), bid, ask, ts_ms)]

    if event_type == "price_change":
        changes = payload.get("price_changes") or payload.get("priceChanges") or []
        out = []
        for ch in changes:
            token = ch.get("asset_id") or ch.get("token_id") or ch.get("tokenId")
            bid = ch.get("best_bid") or ch.get("bestBid")
            ask = ch.get("best_ask") or ch.get("bestAsk")
            if token:
                out.append((str(token), float(bid) if bid else None, float(ask) if ask else None, ts_ms))
        return out

    return []


async def record_market(
    market: BinaryMarket,
    out: Path,
    duration_s: float | None = None,
) -> None:
    books: dict[str, TopOfBook] = {}
    recorder = RatioRecorder(out, market.slug, market.a_label, market.aprime_label)
    started = time.monotonic()

    print(f"{market.question}")
    print(f"A={market.a_label}  A'={market.aprime_label}")
    print(f"recording to {out} — Ctrl+C to stop")

    try:
        async with websockets.connect(WS, ping_interval=None, close_timeout=3) as ws:
            await ws.send(json.dumps({
                "assets_ids": [market.a_token, market.aprime_token],
                "type": "market",
                "custom_feature_enabled": True,
            }))

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(10)
                    await ws.send("PING")

            hb = asyncio.create_task(heartbeat())
            try:
                while True:
                    if duration_s is not None and time.monotonic() - started >= duration_s:
                        break
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    if raw == "PONG":
                        continue
                    recv_ts_ms = time.time_ns() // 1_000_000
                    parsed = json.loads(raw)
                    messages = parsed if isinstance(parsed, list) else [parsed]
                    changed = False
                    source_ts: int | None = None
                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        for token, bid, ask, ts_ms in _message_updates(msg):
                            if bid is not None and ask is not None and bid > 0 and ask > 0 and bid <= ask:
                                books[token] = TopOfBook(bid, ask)
                                changed = True
                            if ts_ms is not None:
                                source_ts = ts_ms if source_ts is None else max(source_ts, ts_ms)
                    source_ts = recv_ts_ms if source_ts is None else source_ts
                    if changed and market.a_token in books and market.aprime_token in books:
                        row = recorder.write(
                            source_ts,
                            recv_ts_ms,
                            books[market.a_token],
                            books[market.aprime_token],
                        )
                        print(
                            f"\rq=[{row['q_bid']:+.4f}, {row['q_ask']:+.4f}] "
                            f"ball={row['q_ball']:+.4f} mid={row['q_mid']:+.4f}",
                            end="",
                            flush=True,
                        )
            finally:
                hb.cancel()
    finally:
        recorder.close()
        print()
