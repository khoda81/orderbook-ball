from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import socket
import time
from urllib.parse import urlparse

import httpx
from polymarket import AsyncPublicClient, Event, Market
from polymarket.clients._transport import AsyncTransport
from polymarket.errors import RequestRejectedError
import websockets

from .core import TopOfBook
from .io import RatioRecorder

WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LOGGER = logging.getLogger("orderbook_ball.polymarket")


@dataclass(frozen=True)
class BinaryMarket:
    slug: str
    question: str
    condition_id: str
    a_label: str
    aprime_label: str
    a_token: str
    aprime_token: str


@dataclass(frozen=True)
class EventSearchResult:
    event_id: str
    slug: str
    title: str
    subtitle: str
    icon: str
    volume: float | None
    volume_24h: float | None
    liquidity: float | None
    end_date: str
    binary_market_count: int


def _force_ipv4_enabled() -> bool:
    value = os.getenv("ORDERBOOK_BALL_FORCE_IPV4", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


@asynccontextmanager
async def _public_client():
    """Yield the official Polymarket client with an IPv4-bound Gamma transport.

    httpx/httpcore resolves through AnyIO, so monkey-patching socket.getaddrinfo
    isn't sufficient to force IPv4. Binding the transport to 0.0.0.0 makes the
    underlying socket AF_INET and avoids broken IPv6 TLS paths while retaining
    the SDK's typed request builders, pagination, and response parsing.

    This touches the SDK's internal Gamma transport because v0.4 doesn't expose
    transport injection publicly. The dependency is pinned to <0.5 accordingly.
    """
    client = AsyncPublicClient(logger=LOGGER)
    ipv4_http: httpx.AsyncClient | None = None

    if _force_ipv4_enabled():
        gamma_url = client.environment.gamma_url
        # Close the SDK-created Gamma pool before swapping it out.
        await client._ctx.gamma.close()  # type: ignore[attr-defined]
        ipv4_http = httpx.AsyncClient(
            base_url=gamma_url,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=2.0),
            transport=httpx.AsyncHTTPTransport(
                local_address="0.0.0.0",
                retries=1,
                http2=True,
            ),
        )
        gamma = AsyncTransport(base_url=gamma_url, client=ipv4_http, logger=LOGGER)
        object.__setattr__(client._ctx, "gamma", gamma)  # type: ignore[attr-defined]

    try:
        async with client:
            yield client
    finally:
        # AsyncTransport doesn't own a client injected by us, so close it here.
        if ipv4_http is not None:
            await ipv4_http.aclose()


def connect_market_ws(**kwargs):
    """Connect to the public CLOB market stream, forcing IPv4 when requested."""
    if _force_ipv4_enabled():
        kwargs.setdefault("family", socket.AF_INET)
    return websockets.connect(WS, **kwargs)


def _slug_from_input(value: str) -> str:
    if "://" in value:
        path = urlparse(value).path.rstrip("/")
        return path.split("/")[-1]
    return value.strip().rstrip("/").split("/")[-1]


def _market_from_sdk(market: Market) -> BinaryMarket | None:
    yes = market.outcomes.yes
    no = market.outcomes.no
    if yes.token_id is None or no.token_id is None:
        return None
    return BinaryMarket(
        slug=str(market.slug or market.id),
        question=str(market.question or market.group_item_title or ""),
        condition_id=str(market.condition_id or ""),
        a_label=str(yes.label),
        aprime_label=str(no.label),
        a_token=str(yes.token_id),
        aprime_token=str(no.token_id),
    )


def _is_live_binary_market(market: Market) -> bool:
    state = market.state
    if state.closed is True or state.archived is True:
        return False
    if state.active is False or state.enable_order_book is False:
        return False
    return _market_from_sdk(market) is not None


def _event_icon(event: Event) -> str:
    for optimized in (event.display.icon_optimized, event.display.image_optimized):
        if optimized is not None:
            url = optimized.image_url_optimized or optimized.image_url_source
            if url:
                return str(url)
    return str(event.icon or event.image or "")


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _search_result_from_sdk(event: Event) -> EventSearchResult | None:
    binary_count = sum(1 for market in event.markets if _is_live_binary_market(market))
    if binary_count == 0 or not event.slug:
        return None

    end_date = event.schedule.end_date
    return EventSearchResult(
        event_id=str(event.id),
        slug=str(event.slug),
        title=str(event.title or "Untitled event"),
        subtitle=str(event.subtitle or event.category or ""),
        icon=_event_icon(event),
        volume=_to_float(event.metrics.volume),
        volume_24h=_to_float(event.metrics.volume_24hr),
        liquidity=_to_float(event.metrics.liquidity),
        end_date=end_date.isoformat() if end_date is not None else "",
        binary_market_count=binary_count,
    )


async def search_binary_events(query: str, limit: int = 8) -> list[EventSearchResult]:
    """Search active Polymarket events through the official Python SDK."""
    query = query.strip()
    if len(query) < 2:
        return []
    limit = max(1, min(int(limit), 12))
    page_size = min(24, max(limit * 2, 8))

    async with _public_client() as client:
        try:
            first = await client.search(
                q=query,
                events_status="active",
                keep_closed_markets=0,
                search_tags=False,
                search_profiles=False,
                page_size=page_size,
            ).first_page()
        except RequestRejectedError as exc:
            LOGGER.warning(
                "filtered Polymarket search rejected for %r (%s); retrying minimal search",
                query,
                exc,
            )
            first = await client.search(q=query, page_size=page_size).first_page()

    results: list[EventSearchResult] = []
    for bundle in first.items:
        for event in bundle.events:
            result = _search_result_from_sdk(event)
            if result is not None:
                results.append(result)
            if len(results) >= limit:
                return results
    return results


def _event_binary_markets(event: Event, *, live_only: bool = False) -> list[BinaryMarket]:
    out: list[BinaryMarket] = []
    for market in event.markets:
        if live_only and not _is_live_binary_market(market):
            continue
        parsed = _market_from_sdk(market)
        if parsed is not None:
            out.append(parsed)
    return out


async def resolve_binary_markets(value: str) -> list[BinaryMarket]:
    """Resolve a Polymarket market/event URL or slug through the official SDK."""
    value = value.strip()
    if not value:
        raise RuntimeError("empty Polymarket market/event value")

    async with _public_client() as client:
        if "://" in value:
            parsed = urlparse(value)
            segments = [segment for segment in parsed.path.split("/") if segment]
            if len(segments) == 2 and segments[0] == "event":
                event = await client.get_event(url=value)
                candidates = _event_binary_markets(event, live_only=True)
                if candidates:
                    return candidates
                raise RuntimeError(f"no live binary CLOB market found for {value!r}")

            try:
                market = await client.get_market(url=value)
                candidate = _market_from_sdk(market)
                if candidate is not None and _is_live_binary_market(market):
                    return [candidate]
            except RequestRejectedError:
                pass

        slug = _slug_from_input(value)
        market_error: Exception | None = None
        try:
            market = await client.get_market(slug=slug)
            candidate = _market_from_sdk(market)
            if candidate is not None and _is_live_binary_market(market):
                return [candidate]
        except Exception as exc:
            market_error = exc

        try:
            event = await client.get_event(slug=slug)
        except Exception as event_error:
            if market_error is not None:
                raise RuntimeError(
                    f"could not resolve Polymarket market/event {value!r}: {event_error}"
                ) from event_error
            raise

        candidates = _event_binary_markets(event, live_only=True)
        if not candidates:
            raise RuntimeError(f"no live binary CLOB market found for {value!r}")
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
        async with connect_market_ws(ping_interval=None, close_timeout=3) as ws:
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
