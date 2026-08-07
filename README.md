# Order-book ball prototype

A deliberately small prototype for treating price as a **causal selection from a moving bid/ask interval**, rather than collapsing every book snapshot to a midpoint.

For two opposing prediction-market tokens `A` and `A'`, the state variable is

```text
q = log(A / A')
```

and the executable ratio spread is

```text
q_bid = log(bid_A / ask_A')
q_ask = log(ask_A / bid_A')
```

The ball obeys the minimal-motion rule

```text
q_ball[t] = clip(q_ball[t-1], q_bid[t], q_ask[t])
```

so it only moves when one edge of the executable interval reaches it.

## Run

With `uv`, the live recorder and web UI only need the lightweight base install:

```bash
uv sync
```

For offline Matplotlib plots:

```bash
uv sync --extra plot
```

For the OpenMarket historical demo (PyArrow + Hugging Face):

```bash
uv sync --extra history
```

### 1. Immediate synthetic sanity check

```bash
uv run obball synthetic --out synthetic.png
```

### 2. Real historical Polymarket data, no recording wait

This downloads only the tiny public OpenMarket sample (not the 15+ GB full corpus), reconstructs the two opposing top-of-books by carrying each token's most recent quote forward, then plots the ratio spread and ball.

```bash
uv run obball openmarket-demo --out openmarket_demo.png
```

The OpenMarket sample is BTC Up/Down data, but mathematically Up/Down and Yes/No are the same two-opposing-token setup.

### 3. Record any live Polymarket binary market

Paste a market/event URL or slug:

```bash
uv run obball live 'https://polymarket.com/event/<slug>' --out data/live.csv
```

Stop with Ctrl+C, then:

```bash
uv run obball plot data/live.csv --out live.png
```

If the event URL contains several binary submarkets, the command prints them and asks you to rerun with `--market-index N`.

You can bypass market discovery entirely:

```bash
uv run obball live \
  --a-token '<YES_TOKEN_ID>' \
  --aprime-token '<NO_TOKEN_ID>' \
  --a-label YES --aprime-label NO \
  --out data/live.csv
```

## What the figure shows

Top panel:

- executable `log(A/A')` bid/ask interval
- midpoint of that *ratio interval*
- `log(mid(A)/mid(A'))` as a separate memoryless baseline
- causal ball (`clip(previous, bid, ask)`)

Bottom panel:

A discretized **temporal spread**. For every `q` level currently inside the spread, it shows the number of seconds since that level was most recently outside the spread. In other words, the vertical price gap is represented as a field of backward hitting times.

## Data / API notes

Polymarket's public CLOB WebSocket currently exposes full `book`, `price_change`, and optional `best_bid_ask` events without authentication. This prototype subscribes to both token IDs and records only paired top-of-book state; it intentionally keeps the raw token quotes in the CSV so alternative estimators can be replayed later.

Polymarket's old high-frequency `/orderbook-history` endpoint was decommissioned in February 2026, so for arbitrary markets recording the WebSocket remains the reliable route. A public OpenMarket release now provides historical millisecond top-of-book data for BTC binary markets and is used by `openmarket-demo`.

## First experiments worth doing

1. Does `q_ball` predict the next executable interval/mid move better than memoryless midpoints?
2. Does ball displacement carry more directional information than book imbalance alone?
3. Are temporal-spread ages predictive? E.g. does a long-lived interior level behave differently when a wall finally reaches it?
4. Compare horizons in **event count** as well as wall-clock time; irregular quote arrival matters here.
5. Keep the comparison causal: at timestamp `t`, only use book state observed by `t`.

## Web UI

Launch the browser version with one command:

```bash
uv run obball web
```

It opens `http://127.0.0.1:8765`. Paste any Polymarket market/event URL or slug. If an event contains several binary submarkets, choose one from the dropdown and connect.

The browser UI includes:

- live executable `q = log(A/A')` bid/ask interval
- causal ball, ratio-spread midpoint, and ratio-of-token-midpoints
- temporal-spread heatmap
- **Linear Δt / `log(1 + Δt)` heatmap toggle**
- rolling display-window selector (the underlying capture is kept independently)
- CSV export of the paired top-of-book stream

The web app uses a tiny local FastAPI bridge rather than connecting to Polymarket directly from JavaScript. This keeps market discovery and the upstream WebSocket in one place and avoids browser CORS/origin differences.

### Heatmap scaling

The original Python plot is **linear in seconds**. It clips the top of the color range at the 97th percentile of finite ages, which can make it look somewhat compressed even though it is not logarithmic.

For an offline plot you can now choose either scale explicitly:

```bash
uv run obball plot data/live.csv --heatmap-scale linear --out live-linear.png
uv run obball plot data/live.csv --heatmap-scale log --out live-log.png
```

The log mode visualizes `log(1 + Δt)`; `log1p` keeps zero mapped exactly to zero and behaves sensibly for sub-second ages.
