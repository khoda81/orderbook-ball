from __future__ import annotations

import os
import socket


def _force_polymarket_ipv4_dns() -> None:
    """Prefer IPv4 only for Polymarket hosts inside this process.

    Some VPN/tunnel setups advertise working IPv6 routes but terminate the TLS
    handshake for Polymarket's Cloudflare IPv6 addresses. A TLS EOF doesn't
    trigger the usual IPv6 -> IPv4 connection fallback, so HTTP clients fail
    even though the IPv4 endpoint is healthy.

    Keep the workaround tightly scoped to *.polymarket.com and allow opting out
    with ORDERBOOK_BALL_FORCE_IPV4=0.
    """
    enabled = os.getenv("ORDERBOOK_BALL_FORCE_IPV4", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return

    original = socket.getaddrinfo
    if getattr(original, "_orderbook_ball_force_ipv4", False):
        return

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if (
            isinstance(host, str)
            and (host == "polymarket.com" or host.endswith(".polymarket.com"))
            and family in {0, socket.AF_UNSPEC}
        ):
            family = socket.AF_INET
        return original(host, port, family, type, proto, flags)

    getaddrinfo._orderbook_ball_force_ipv4 = True  # type: ignore[attr-defined]
    socket.getaddrinfo = getaddrinfo


_force_polymarket_ipv4_dns()

from .core import RatioInterval, TopOfBook, clip_ball, ratio_interval, temporal_spread_age

__all__ = ["RatioInterval", "TopOfBook", "clip_ball", "ratio_interval", "temporal_spread_age"]
