"""Offline tests for inverse-volatility position sizing."""
from agents.allocator.agent import (
    SIZING_MAX_FRAC,
    SIZING_MIN_FRAC,
    _inverse_vol_budgets,
)


def _cands(vols):
    return [{"ticker": f"T{i}", "vol20": v} for i, v in enumerate(vols)]


def test_sums_to_remaining_and_low_vol_gets_more():
    remaining = 240.0
    cands = _cands([0.01, 0.02, 0.04])  # low, mid, high vol
    b = _inverse_vol_budgets(cands, remaining, "core")
    assert abs(sum(b) - remaining) < 1e-3, sum(b)       # fully deployed
    assert b[0] >= b[1] >= b[2]                          # non-increasing in vol
    assert b[0] > b[2]                                   # lowest vol funded more than highest
    lo, hi = SIZING_MIN_FRAC["core"] * remaining, SIZING_MAX_FRAC["core"] * remaining
    for x in b:
        assert lo - 1e-6 <= x <= hi + 1e-6, x           # respects caps
        assert x >= 1.0                                  # clears $1 notional


def test_single_candidate_gets_all():
    assert _inverse_vol_budgets(_cands([0.03]), 45.0, "swing") == [45.0]


def test_empty():
    assert _inverse_vol_budgets([], 100.0, "core") == []


def test_two_swing_names_fundable():
    remaining = 45.0
    b = _inverse_vol_budgets(_cands([0.05, 0.03]), remaining, "swing")
    assert abs(sum(b) - remaining) < 1e-3
    assert all(x >= 1.0 for x in b)


def test_two_core_names_fully_deployed():
    # With only 2 core names the static 0.35 cap would leave 30% idle; the
    # effective cap must straddle 0.5 so the whole bucket deploys.
    remaining = 95.0
    b = _inverse_vol_budgets(_cands([0.05, 0.05]), remaining, "core")
    assert abs(sum(b) - remaining) < 1e-3, sum(b)


def run():
    test_sums_to_remaining_and_low_vol_gets_more()
    test_single_candidate_gets_all()
    test_empty()
    test_two_swing_names_fundable()
    test_two_core_names_fully_deployed()
