"""Offline tests for the broker-side protective stop level (pure function)."""
from agents.stops import protective_stop_price


def test_core_hard_floor():
    # Core -15%: entry 100, never armed -> stop at 85.
    assert protective_stop_price("core", 100.0, 0.0) == 85.0


def test_core_trailing_raises_floor():
    # Peak +30 armed (>=15): trail level = entry * (1 + (30-25)/100) = 105,
    # above the 85 hard floor -> the stop locks in gains.
    assert protective_stop_price("core", 100.0, 30.0) == 105.0


def test_core_below_arm_keeps_hard_floor():
    # Peak +10 hasn't armed the +15 trail -> plain hard floor.
    assert protective_stop_price("core", 100.0, 10.0) == 85.0


def test_swing_floor_and_trail():
    assert protective_stop_price("swing", 100.0, 0.0) == 92.0
    # Peak +10 armed (>=6): 100*(1+(10-7)/100) = 103 > 92.
    assert protective_stop_price("swing", 100.0, 10.0) == 103.0


def test_lottery_new_floor():
    # Tightened floor -40 (was -60).
    assert protective_stop_price("lottery", 100.0, 0.0) == 60.0
    # Peak +16 armed (>=15): 100*(1+(16-25)/100) = 91 > 60.
    assert protective_stop_price("lottery", 100.0, 16.0) == 91.0


def test_unknown_bucket_or_bad_entry():
    assert protective_stop_price("nope", 100.0, 0.0) is None
    assert protective_stop_price("core", 0.0, 0.0) is None


def run():
    test_core_hard_floor()
    test_core_trailing_raises_floor()
    test_core_below_arm_keeps_hard_floor()
    test_swing_floor_and_trail()
    test_lottery_new_floor()
    test_unknown_bucket_or_bad_entry()
