"""Offline truth-table tests for the exit decision logic."""
from agents.exits import _decide, effective_hard_stop_pct


def _p(pl):
    return {"pl_pct": pl}


def test_hard_stop_floor():
    # Core -16% trips the -15 hard stop regardless of peak.
    d = _decide(_p(-16), "core", 0, 0.0, False, False)
    assert d and d[0] == 1.0 and "hard stop" in d[1]


def test_atr_stop_only_tightens():
    # Calm name (ATR 3%): stop tightens to -7.5 (2.5×3). Wild name (ATR 10%):
    # 2.5×10 = -25 would LOOSEN past the -15 floor -> floor holds.
    assert effective_hard_stop_pct("core", 3.0) == -7.5
    assert effective_hard_stop_pct("core", 10.0) == -15.0
    # No recorded ATR (legacy position) -> flat bucket stop, unchanged behavior.
    assert effective_hard_stop_pct("core", None) == -15.0


def test_atr_stop_in_decide():
    # -9% on a calm core name (ATR 3% -> stop -7.5) sells; without ATR it holds.
    d = _decide(_p(-9), "core", 0, 0.0, False, False, atr_pct=3.0)
    assert d and d[0] == 1.0 and "hard stop" in d[1]
    assert _decide(_p(-9), "core", 0, 0.0, False, False) is None


def test_trailing_fires_when_armed_and_given_back():
    # Core arms at +15, trails 25 from peak. Peak +40%, now +10% (gave back 30) -> exit.
    d = _decide(_p(10), "core", 0, 40.0, False, False)
    assert d and d[0] == 1.0 and "trailing" in d[1]


def test_trailing_silent_below_arm():
    # Peak +10% never armed (core arm +15) -> hold.
    assert _decide(_p(8), "core", 0, 10.0, False, False) is None


def test_trailing_silent_near_peak():
    # Within 25 of the +30 peak -> let it run.
    assert _decide(_p(10), "core", 0, 30.0, False, False) is None


def test_swing_take_profit():
    d = _decide(_p(21), "swing", 1, 21.0, False, False)
    assert d and d[0] == 1.0 and "profit target" in d[1]


def test_lottery_trim_once_then_suppressed():
    first = _decide(_p(55), "lottery", 1, 55.0, False, False)
    assert first and first[0] == 0.5 and "trim" in first[1]
    # Already trimmed, still within 25 of the +55 peak -> no further action.
    assert _decide(_p(55), "lottery", 1, 55.0, False, True) is None


def test_lottery_hard_stop_tightened():
    # -41% trips the new -40 lottery floor (was -60).
    d = _decide(_p(-41), "lottery", 1, 0.0, False, False)
    assert d and d[0] == 1.0 and "hard stop" in d[1]
    # -34% alone (young position, no peak) does NOT trip the floor.
    assert _decide(_p(-34), "lottery", 1, 0.0, False, False) is None


def test_lottery_trail_arms_at_15():
    # Peak +16 arms the trail; -10 now is >25 below peak -> exit (the WULF
    # round-trip: +15.7 peak to -30 with the old +60 arm never triggered).
    d = _decide(_p(-10), "lottery", 1, 16.0, False, False)
    assert d and d[0] == 1.0 and "trailing" in d[1]
    # Peak +10 never armed -> hold.
    assert _decide(_p(-10), "lottery", 1, 10.0, False, False) is None


def test_lottery_time_stop():
    # 22 calendar days (> 15 trading * 1.4) and never reached +20 -> exit.
    d = _decide(_p(-5), "lottery", 22, 5.0, False, False)
    assert d and d[0] == 1.0 and "time stop" in d[1]
    # Same age but it DID pop (+25 peak) -> the trailing stop governs, not time.
    assert _decide(_p(5), "lottery", 22, 25.0, False, False) is None
    # Young position, hasn't popped yet -> hold.
    assert _decide(_p(-5), "lottery", 10, 5.0, False, False) is None


def test_core_trend_break_disabled():
    # Core trend-break is intentionally OFF (let winners run) -> no exit on it alone.
    assert _decide(_p(2), "core", 0, 2.0, True, False) is None


def test_swing_max_hold_flat_sells():
    # Past max hold and flat (<= +1) -> dead capital, sell.
    d = _decide(_p(0.5), "swing", 15, 2.0, False, False)
    assert d and d[0] == 1.0 and "max hold" in d[1]


def test_swing_max_hold_winner_rides():
    # Past max hold but a WINNER near its peak -> no force-sell anymore
    # (the old rule 6 systematically cut the book's best trades here).
    assert _decide(_p(12), "swing", 15, 13.0, False, False) is None


def test_swing_stale_trail():
    # Past max hold, winner gave back >= 5 from peak -> stale trail exits.
    d = _decide(_p(7), "swing", 15, 13.0, False, False)
    assert d and d[0] == 1.0 and "stale trail" in d[1]


def test_plain_hold():
    assert _decide(_p(3), "core", 0, 3.0, False, False) is None


def run():
    test_hard_stop_floor()
    test_atr_stop_only_tightens()
    test_atr_stop_in_decide()
    test_trailing_fires_when_armed_and_given_back()
    test_trailing_silent_below_arm()
    test_trailing_silent_near_peak()
    test_swing_take_profit()
    test_lottery_trim_once_then_suppressed()
    test_lottery_hard_stop_tightened()
    test_lottery_trail_arms_at_15()
    test_lottery_time_stop()
    test_core_trend_break_disabled()
    test_swing_max_hold_flat_sells()
    test_swing_max_hold_winner_rides()
    test_swing_stale_trail()
    test_plain_hold()
