import math
import numpy as np

from orderbook_ball.core import TopOfBook, clip_ball, ratio_interval, temporal_spread_age


def test_ratio_interval():
    a = TopOfBook(0.58, 0.60)
    b = TopOfBook(0.40, 0.42)
    r = ratio_interval(a, b)
    assert math.isclose(r.q_bid, math.log(0.58 / 0.42))
    assert math.isclose(r.q_ask, math.log(0.60 / 0.40))


def test_ball_hysteresis():
    r0 = type("R", (), {"q_bid": -1.0, "q_ask": 1.0, "midpoint": 0.0})()
    assert clip_ball(None, r0) == 0.0
    r1 = type("R", (), {"q_bid": -0.5, "q_ask": 0.8})()
    assert clip_ball(0.2, r1) == 0.2
    r2 = type("R", (), {"q_bid": 0.4, "q_ask": 0.8})()
    assert clip_ball(0.2, r2) == 0.4
    r3 = type("R", (), {"q_bid": -0.8, "q_ask": 0.1})()
    assert clip_ball(0.4, r3) == 0.1


def test_temporal_age():
    ts = np.array([0, 1000, 2000, 3000])
    lo = np.array([-0.2, -0.2, -1.0, -1.0])
    hi = np.array([0.2, 0.2, 1.0, 1.0])
    grid = np.array([0.5])
    age = temporal_spread_age(ts, lo, hi, grid)
    assert age[0, 0] == 0
    assert age[1, 0] == 0
    assert age[2, 0] == 1
    assert age[3, 0] == 2
