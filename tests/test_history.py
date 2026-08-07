import math

from orderbook_ball.history import _pair_history
from orderbook_ball.polymarket import BinaryMarket


def _market() -> BinaryMarket:
    return BinaryMarket(
        slug="example",
        question="Example?",
        condition_id="0x" + "ab" * 32,
        a_label="Yes",
        aprime_label="No",
        a_token="yes-token",
        aprime_token="no-token",
    )


def test_pair_history_reconstructs_ratio_and_ball():
    market = _market()
    raw = [
        (1000, 1010, "yes-token", 0.58, 0.60),
        (1100, 1110, "no-token", 0.40, 0.42),
        # Only Yes changes; the No top of book should be carried forward.
        (1200, 1210, "yes-token", 0.61, 0.63),
    ]

    rows = _pair_history(raw, market, max_rows=100)
    assert len(rows) == 2
    assert rows[0]["source"] == "pmxt-v2"
    assert rows[0]["historical"] is True
    assert math.isclose(rows[0]["q_bid"], math.log(0.58 / 0.42))
    assert math.isclose(rows[0]["q_ask"], math.log(0.60 / 0.40))

    # The first ball initializes at the interval midpoint; the later interval
    # pushes it only if that midpoint is no longer feasible.
    first_ball = rows[0]["q_ball"]
    assert rows[1]["q_ball"] >= first_ball
    assert rows[1]["aprime_bid"] == 0.40
    assert rows[1]["aprime_ask"] == 0.42


def test_pair_history_caps_to_most_recent_rows():
    market = _market()
    raw = [(1000, 1000, "no-token", 0.40, 0.42)]
    for i in range(10):
        raw.append((1100 + i, 1100 + i, "yes-token", 0.58 + i * 0.001, 0.60 + i * 0.001))

    rows = _pair_history(raw, market, max_rows=3)
    assert len(rows) == 3
    assert rows[-1]["ts_ms"] == 1109
