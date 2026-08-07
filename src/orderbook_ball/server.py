from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .core import TopOfBook, clip_ball, naive_ratio_of_mids, ratio_interval
from .history import load_pmxt_history
from .polymarket import (
    BinaryMarket,
    EventSearchResult,
    _message_updates,
    connect_market_ws,
    resolve_binary_markets,
    search_binary_events,
)

WEB_DIR = Path(__file__).with_name("web")
LOGGER = logging.getLogger("uvicorn.error")


def _market_json(m: BinaryMarket) -> dict[str, str]:
    return {
        "slug": m.slug,
        "question": m.question,
        "condition_id": m.condition_id,
        "a_label": m.a_label,
        "aprime_label": m.aprime_label,
        "a_token": m.a_token,
        "aprime_token": m.aprime_token,
    }


def _search_json(event: EventSearchResult) -> dict[str, object]:
    return {
        "id": event.event_id,
        "slug": event.slug,
        "title": event.title,
        "subtitle": event.subtitle,
        "icon": event.icon,
        "volume": event.volume,
        "volume_24h": event.volume_24h,
        "liquidity": event.liquidity,
        "end_date": event.end_date,
        "binary_market_count": event.binary_market_count,
    }


def _market_from_config(config: dict) -> BinaryMarket:
    return BinaryMarket(
        slug=str(config.get("slug") or "manual"),
        question=str(config.get("question") or ""),
        condition_id=str(config.get("condition_id") or ""),
        a_label=str(config.get("a_label") or "A"),
        aprime_label=str(config.get("aprime_label") or "A'"),
        a_token=str(config["a_token"]),
        aprime_token=str(config["aprime_token"]),
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Order-book ball")

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/app.js")
    async def js():
        return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")

    @app.get("/search.js")
    async def search_js():
        return FileResponse(WEB_DIR / "search.js", media_type="text/javascript")

    @app.get("/styles.css")
    async def css():
        return FileResponse(WEB_DIR / "styles.css", media_type="text/css")

    @app.get("/api/search")
    async def search(q: str, limit: int = 8):
        query = q.strip()
        if len(query) < 2:
            return {"events": []}
        try:
            found = await search_binary_events(query, limit=limit)
        except Exception as exc:  # Upstream discovery boundary.
            LOGGER.exception("Polymarket search failed for query=%r limit=%s", query, limit)
            detail = f"Polymarket search failed: {type(exc).__name__}: {exc}"
            raise HTTPException(status_code=502, detail=detail) from exc
        return {"events": [_search_json(event) for event in found]}

    @app.get("/api/markets")
    async def markets(value: str):
        try:
            found = await resolve_binary_markets(value)
        except Exception as exc:  # API boundary: return a useful browser error.
            LOGGER.exception("Polymarket market resolution failed for value=%r", value)
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
        return {"markets": [_market_json(m) for m in found]}

    @app.post("/api/history")
    async def history(config: dict):
        """Best-effort PMXT archive bootstrap; never blocks the live path on failure."""
        try:
            market = _market_from_config(config)
            if not market.condition_id:
                return {"source": "pmxt-v2", "rows": [], "error": "market has no condition id"}
            result = await load_pmxt_history(
                market,
                max_rows=int(config.get("max_rows") or 6000),
                archive_files=int(config.get("archive_files") or 6),
            )
            return {
                "source": result.source,
                "rows": result.rows,
                "archive_file_count": result.archive_file_count,
                "newest_archive_hour": result.newest_archive_hour,
            }
        except Exception as exc:
            # History is a convenience/bootstrap. A live market should remain fully
            # usable even if PMXT, DuckDB/httpfs, or the user's network is unhappy.
            LOGGER.warning(
                "PMXT history bootstrap failed for market=%r: %s",
                config.get("slug") or config.get("condition_id"),
                exc,
                exc_info=True,
            )
            return {
                "source": "pmxt-v2",
                "rows": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

    @app.websocket("/ws")
    async def stream(ws: WebSocket):
        await ws.accept()
        upstream = None
        heartbeat = None
        try:
            config = await asyncio.wait_for(ws.receive_json(), timeout=20)
            market = _market_from_config(config)

            await ws.send_json({"type": "status", "state": "connecting"})
            upstream = await connect_market_ws(ping_interval=None, close_timeout=3)
            await upstream.send(json.dumps({
                "assets_ids": [market.a_token, market.aprime_token],
                "type": "market",
                "custom_feature_enabled": True,
            }))

            async def upstream_heartbeat() -> None:
                while True:
                    await asyncio.sleep(10)
                    await upstream.send("PING")

            heartbeat = asyncio.create_task(upstream_heartbeat())
            await ws.send_json({"type": "status", "state": "connected"})

            books: dict[str, TopOfBook] = {}
            ball: float | None = None
            sequence = 0

            while True:
                raw = await asyncio.wait_for(upstream.recv(), timeout=20)
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
                if not changed or market.a_token not in books or market.aprime_token not in books:
                    continue

                a = books[market.a_token]
                ap = books[market.aprime_token]
                interval = ratio_interval(a, ap)
                ball = clip_ball(ball, interval)
                sequence += 1
                await ws.send_json({
                    "type": "tick",
                    "source": "live",
                    "historical": False,
                    "sequence": sequence,
                    "ts_ms": source_ts,
                    "recv_ts_ms": recv_ts_ms,
                    "market": market.slug,
                    "a_label": market.a_label,
                    "aprime_label": market.aprime_label,
                    "a_bid": a.bid,
                    "a_ask": a.ask,
                    "aprime_bid": ap.bid,
                    "aprime_ask": ap.ask,
                    "q_bid": interval.q_bid,
                    "q_ask": interval.q_ask,
                    "q_mid": interval.midpoint,
                    "q_ratio_of_mids": naive_ratio_of_mids(a, ap),
                    "q_ball": ball,
                })
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Polymarket live stream failed")
            try:
                await ws.send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            if upstream is not None:
                try:
                    await upstream.close()
                except Exception:
                    pass

    return app


app = create_app()


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)
