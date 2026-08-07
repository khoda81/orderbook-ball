from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .polymarket import BinaryMarket, record_market, resolve_binary_market


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="obball", description="Order-book ball / temporal-spread prototype")
    sub = p.add_subparsers(dest="cmd", required=True)

    live = sub.add_parser("live", help="record a live Polymarket binary market")
    live.add_argument("market", nargs="?", help="Polymarket URL or market/event slug")
    live.add_argument("--market-index", type=int)
    live.add_argument("--a-token")
    live.add_argument("--aprime-token")
    live.add_argument("--a-label", default="A")
    live.add_argument("--aprime-label", default="A'")
    live.add_argument("--out", type=Path, default=Path("data/live.csv"))
    live.add_argument("--duration", type=float, help="seconds; omit to run until Ctrl+C")

    plot = sub.add_parser("plot", help="plot a recorded CSV (requires the plot extra)")
    plot.add_argument("csv", type=Path)
    plot.add_argument("--out", type=Path, default=Path("ball.png"))
    plot.add_argument("--heatmap-scale", choices=["linear", "log"], default="linear")

    demo = sub.add_parser("openmarket-demo", help="download the tiny public OpenMarket sample (requires history extra)")
    demo.add_argument("--market-slug")
    demo.add_argument("--csv", type=Path, default=Path("data/openmarket_demo.csv"))
    demo.add_argument("--out", type=Path, default=Path("openmarket_demo.png"))
    demo.add_argument("--heatmap-scale", choices=["linear", "log"], default="linear")

    synth = sub.add_parser("synthetic", help="generate a local synthetic sanity-check plot (requires plot extra)")
    synth.add_argument("--csv", type=Path, default=Path("data/synthetic.csv"))
    synth.add_argument("--out", type=Path, default=Path("synthetic.png"))
    synth.add_argument("--seed", type=int, default=7)
    synth.add_argument("--heatmap-scale", choices=["linear", "log"], default="linear")

    web = sub.add_parser("web", help="launch the live browser UI")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    return p


async def _live(args) -> None:
    if args.a_token and args.aprime_token:
        market = BinaryMarket(
            slug="manual",
            question="manual token pair",
            condition_id="",
            a_label=args.a_label,
            aprime_label=args.aprime_label,
            a_token=args.a_token,
            aprime_token=args.aprime_token,
        )
    elif args.market:
        market = await resolve_binary_market(args.market, args.market_index)
    else:
        raise SystemExit("give a Polymarket URL/slug, or both --a-token and --aprime-token")
    await record_market(market, args.out, args.duration)


def main() -> None:
    args = _parser().parse_args()
    if args.cmd == "live":
        try:
            asyncio.run(_live(args))
        except KeyboardInterrupt:
            pass
    elif args.cmd == "plot":
        try:
            from .plotting import plot_recording
        except ImportError as exc:
            raise SystemExit("plot dependencies missing; run: uv sync --extra plot") from exc
        plot_recording(args.csv, args.out, heatmap_scale=args.heatmap_scale)
        print(args.out)
    elif args.cmd == "openmarket-demo":
        try:
            from .openmarket import make_demo_csv
            from .plotting import plot_recording
        except ImportError as exc:
            raise SystemExit("history dependencies missing; run: uv sync --extra history") from exc
        make_demo_csv(args.csv, args.market_slug)
        plot_recording(args.csv, args.out, heatmap_scale=args.heatmap_scale)
        print(args.out)
    elif args.cmd == "synthetic":
        try:
            from .plotting import plot_recording
            from .synthetic import generate
        except ImportError as exc:
            raise SystemExit("plot dependencies missing; run: uv sync --extra plot") from exc
        generate(args.csv, seed=args.seed)
        plot_recording(args.csv, args.out, heatmap_scale=args.heatmap_scale)
        print(args.out)
    elif args.cmd == "web":
        from .server import run
        run(host=args.host, port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
