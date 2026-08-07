from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urljoin

import duckdb
import httpx

from .core import TopOfBook, clip_ball, naive_ratio_of_mids, ratio_interval
from .polymarket import BinaryMarket

LOGGER = logging.getLogger("orderbook_ball.history")
PMXT_INDEX = "https://archive.pmxt.dev/Polymarket/v2"
_FILE_RE = re.compile(
    r'href=["\']([^"\']*polymarket_orderbook_\d{4}-\d{2}-\d{2}T\d{2}\.parquet)["\']',
    re.IGNORECASE,
)
_INDEX_CACHE_TTL_S = 300
_index_cache_at = 0.0
_index_cache_urls: list[str] = []
_proxy_lock = threading.Lock()
_proxy_server: socketserver.ThreadingTCPServer | None = None
_proxy_url: str | None = None


@dataclass(frozen=True)
class HistoryBootstrap:
    source: str
    rows: list[dict[str, object]]
    archive_file_count: int
    newest_archive_hour: str | None


def _quote_sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _connect_ipv4(host: str, port: int) -> socket.socket:
    """Open one TCP connection using IPv4 only."""
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        host,
        port,
        socket.AF_INET,
        socket.SOCK_STREAM,
    ):
        upstream = socket.socket(family, socktype, proto)
        upstream.settimeout(10)
        try:
            upstream.connect(sockaddr)
            upstream.settimeout(None)
            return upstream
        except OSError as exc:
            last_error = exc
            upstream.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"no IPv4 address found for {host!r}")


class _IPv4ConnectProxyHandler(socketserver.BaseRequestHandler):
    """Tiny localhost CONNECT proxy used only for PMXT HTTPS range reads.

    DuckDB/httpfs currently doesn't expose libcurl's CURLOPT_IPRESOLVE. Routing
    HTTPS through this local CONNECT proxy leaves TLS, SNI, certificate checks,
    HEAD requests and byte ranges entirely inside DuckDB/libcurl, while forcing
    only the proxy -> PMXT TCP leg to AF_INET.
    """

    def handle(self) -> None:
        client = self.request
        upstream: socket.socket | None = None
        try:
            client.settimeout(10)
            request = bytearray()
            while b"\r\n\r\n" not in request:
                chunk = client.recv(4096)
                if not chunk:
                    return
                request.extend(chunk)
                if len(request) > 64 * 1024:
                    client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                    return

            header, buffered = bytes(request).split(b"\r\n\r\n", 1)
            first_line = header.split(b"\r\n", 1)[0].decode("ascii", "replace")
            parts = first_line.split(" ")
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return

            target = parts[1]
            host, separator, port_text = target.rpartition(":")
            host = host.strip("[]").lower()
            if not separator or not port_text.isdigit():
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            port = int(port_text)

            # This isn't a general-purpose local proxy. Restrict it to the public
            # PMXT archive domains that the history bootstrap intentionally reads.
            if port != 443 or not (host == "pmxt.dev" or host.endswith(".pmxt.dev")):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return

            upstream = _connect_ipv4(host, port)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if buffered:
                upstream.sendall(buffered)

            client.settimeout(None)
            while True:
                readable, _, _ = select.select([client, upstream], [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    payload = source.recv(64 * 1024)
                    if not payload:
                        return
                    destination = upstream if source is client else client
                    destination.sendall(payload)
        except (OSError, ValueError):
            # DuckDB reports the useful HTTP/TLS error to the caller. Don't print
            # an extra traceback from a daemon proxy thread for the same failure.
            return
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class _IPv4ConnectProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _pmxt_ipv4_proxy_url() -> str:
    """Start the process-local IPv4 CONNECT proxy lazily and return its URL."""
    global _proxy_server, _proxy_url
    with _proxy_lock:
        if _proxy_url is not None:
            return _proxy_url

        server = _IPv4ConnectProxyServer(("127.0.0.1", 0), _IPv4ConnectProxyHandler)
        thread = threading.Thread(
            target=server.serve_forever,
            name="orderbook-ball-pmxt-ipv4-proxy",
            daemon=True,
        )
        thread.start()
        _proxy_server = server
        _proxy_url = f"http://127.0.0.1:{server.server_address[1]}"
        LOGGER.info("PMXT DuckDB range reads use IPv4 CONNECT proxy %s", _proxy_url)
        return _proxy_url


async def _recent_pmxt_urls(max_files: int = 2) -> list[str]:
    """Return newest PMXT v2 hourly Parquet objects from the public index."""
    global _index_cache_at, _index_cache_urls
    now = time.monotonic()
    if _index_cache_urls and now - _index_cache_at < _INDEX_CACHE_TTL_S:
        return _index_cache_urls[:max_files]

    # The index lookup itself uses httpx, where we can bind IPv4 directly.
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=2.0),
        follow_redirects=True,
    ) as client:
        response = await client.get(PMXT_INDEX)
        response.raise_for_status()

    urls: list[str] = []
    for href in _FILE_RE.findall(response.text):
        url = urljoin(PMXT_INDEX + "/", href)
        if url not in urls:
            urls.append(url)

    # The archive page is newest-first today, but derive order from the filename
    # rather than depending on presentation HTML.
    urls.sort(reverse=True)
    _index_cache_urls = urls
    _index_cache_at = now
    return urls[:max_files]


def _query_pmxt_rows(
    urls: list[str],
    market: BinaryMarket,
    raw_limit: int,
) -> list[tuple[int, int, str, float, float]]:
    """Read only this market/token pair from PMXT's remote Parquet row groups."""
    if not urls:
        return []

    files = ", ".join(_quote_sql_string(url) for url in urls)
    sql = f"""
        SELECT
            epoch_ms(timestamp) AS source_ts_ms,
            epoch_ms(timestamp_received) AS recv_ts_ms,
            asset_id,
            CAST(best_bid AS DOUBLE) AS best_bid,
            CAST(best_ask AS DOUBLE) AS best_ask
        FROM read_parquet([{files}])
        WHERE market = ?
          AND asset_id IN (?, ?)
          AND event_type = 'price_change'
          AND best_bid IS NOT NULL
          AND best_ask IS NOT NULL
        ORDER BY timestamp_received DESC
        LIMIT ?
    """

    con = duckdb.connect(database=":memory:")
    try:
        # Keep DuckDB's remote-Parquet machinery intact: it will HEAD/read the
        # footer, use HTTP Range requests, project only these columns, and prune
        # row groups using PMXT's (market, asset_id, timestamp_received) ordering.
        # The localhost proxy only forces the actual PMXT TCP connection to IPv4.
        proxy_url = _pmxt_ipv4_proxy_url()
        con.execute(f"SET http_proxy = {_quote_sql_string(proxy_url)}")
        con.execute("SET force_download = false")
        con.execute("SET enable_http_metadata_cache = true")
        con.execute("SET http_timeout = 30")
        con.execute("SET http_retries = 2")

        result = con.execute(
            sql,
            [
                market.condition_id.encode("ascii"),
                market.a_token,
                market.aprime_token,
                int(raw_limit),
            ],
        ).fetchall()
    finally:
        con.close()

    out: list[tuple[int, int, str, float, float]] = []
    for source_ts, recv_ts, asset_id, bid, ask in reversed(result):
        if source_ts is None or recv_ts is None or bid is None or ask is None:
            continue
        out.append((int(source_ts), int(recv_ts), str(asset_id), float(bid), float(ask)))
    return out


def _pair_history(
    raw: list[tuple[int, int, str, float, float]],
    market: BinaryMarket,
    max_rows: int,
) -> list[dict[str, object]]:
    books: dict[str, TopOfBook] = {}
    ball: float | None = None
    paired: list[dict[str, object]] = []

    for source_ts, recv_ts, token, bid, ask in raw:
        if bid <= 0 or ask <= 0 or bid > ask:
            continue
        books[token] = TopOfBook(bid, ask)
        if market.a_token not in books or market.aprime_token not in books:
            continue

        a = books[market.a_token]
        ap = books[market.aprime_token]
        interval = ratio_interval(a, ap)
        ball = clip_ball(ball, interval)
        paired.append(
            {
                "type": "tick",
                "source": "pmxt-v2",
                "historical": True,
                "ts_ms": source_ts,
                "recv_ts_ms": recv_ts,
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
            }
        )

    return paired[-max_rows:]


async def load_pmxt_history(
    market: BinaryMarket,
    *,
    max_rows: int = 6000,
    archive_files: int = 2,
) -> HistoryBootstrap:
    """Bootstrap a market with the newest available PMXT millisecond quote history.

    PMXT v2 is an hourly archive, so it intentionally lags the live stream. The
    browser treats this as an archive preview rather than pretending the gap to
    live data was continuously observed.
    """
    max_rows = max(100, min(int(max_rows), 12_000))
    archive_files = max(1, min(int(archive_files), 6))
    urls = await _recent_pmxt_urls(archive_files)
    if not urls:
        return HistoryBootstrap("pmxt-v2", [], 0, None)

    # Ask for extra single-leg rows because two token streams must be paired.
    raw_limit = min(60_000, max_rows * 4)
    raw = await asyncio.to_thread(_query_pmxt_rows, urls, market, raw_limit)
    rows = _pair_history(raw, market, max_rows)

    newest = None
    if urls:
        match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2})\.parquet$", urls[0])
        newest = match.group(1) if match else None

    return HistoryBootstrap(
        source="pmxt-v2",
        rows=rows,
        archive_file_count=len(urls),
        newest_archive_hour=newest,
    )
