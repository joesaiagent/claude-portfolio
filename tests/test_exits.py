"""Offline truth-table tests for the exit decision logic."""
from agents.exits import _decide


def _p(pl):
    return {"pl_pct": pl}


def test_hard_stop_floor():
    # Core -16% trips the -15 hard stop regardless of peak.
    assert _decide(_p(-16), "core", 0, 0.0, False, False) == (1.0, "hard stop (-16.0%)")


def test_trailing_fires_when_armed_and_given_back():
    # Peak +25%, now +10% -> gave back >12 from peak after arming at +8 -> full exit.
    d = _decide(_p(10), "core", 0, 25.0, False, False)
    assert d and d[0] == 1.0 and "trailing" in d[1]


def test_trailing_silent_below_arm():
    # Peak +7% never armed (arm +8) -> hold.
    assert _decide(_p(5), "core", 0, 7.0, False, False) is None


def test_trailing_silent_near_peak():
    # Still within 12 of the +25 peak -> hold.
    assert _decide(_p(20), "core", 0, 25.0, False, False) is None


def test_swing_take_profit():
    d = _decide(_p(21), "swing", 1, 21.0, False, False)
    assert d and d[0] == 1.0 and "profit target" in d[1]


def test_lottery_trim_once_then_suppressed():
    first = _decide(_p(55), "lottery", 1, 55.0, False, False)
    assert first and first[0] == 0.5 and "trim" in first[1]
    # Already trimmed and below the +60 trail arm -> no further action.
    assert _decide(_p(55), "lottery", 1, 55.0, False, True) is None


def test_core_trend_break():
    d = _decide(_p(2), "core", 0, 2.0, True, False)
    assert d and d[0] == 1.0 and "trend break" in d[1]


def test_swing_max_hold():
    d = _decide(_p(3), "swing", 15, 3.0, False, False)
    assert d and d[0] == 1.0 and "max hold" in d[1]


def test_plain_hold():
    assert _decide(_p(3), "core", 0, 3.0, False, False) is None


def run():
    test_hard_stop_floor()
    test_trailing_fires_when_armed_and_given_back()
    test_trailing_silent_below_arm()
    test_trailing_silent_near_peak()
    test_swing_take_profit()
    test_lottery_trim_once_then_suppressed()
    test_core_trend_break()
    test_swing_max_hold()
    test_plain_hold()
