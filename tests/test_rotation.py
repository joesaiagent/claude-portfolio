"""Offline tests for the rotation trigger and its guards."""
from datetime import datetime, timedelta, timezone

from agents.allocator.agent import _rotation_candidates, ROTATION_DAILY_CAP


def _now_minus(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _state(weak_age_days=10, rotations_today=None):
    # Two swing holdings (bucket is "full"); H1 is the weak one.
    log = [
        {"side": "buy", "ticker": "H1", "bucket": "swing", "ts": _now_minus(weak_age_days)},
        {"side": "buy", "ticker": "H2", "bucket": "swing", "ts": _now_minus(20)},
    ]
    s = {"order_log": log}
    if rotations_today is not None:
        s["rotations_today"] = rotations_today
    return s


# Held positions (swing bucket full at 2).
POSITIONS = [
    {"ticker": "H1", "bucket": "swing", "shares": 1.0, "pl_dollars": -0.5},
    {"ticker": "H2", "bucket": "swing", "shares": 1.0, "pl_dollars": 2.0},
]


def _watchlist(fresh_score):
    return [
        {"ticker": "F1", "bucket": "swing", "score": fresh_score},  # fresh candidate
        {"ticker": "H1", "bucket": "swing", "score": 50.0},          # weak holding
        {"ticker": "H2", "bucket": "swing", "score": 70.0},          # strong holding
    ]


def test_fires_when_all_conditions_met():
    actions, rot = _rotation_candidates(_state(), _watchlist(80.0), POSITIONS)
    assert len(actions) == 1
    a = actions[0]
    assert a["sell"]["ticker"] == "H1" and a["buy"]["ticker"] == "F1"
    assert rot["swing"] == 1


def test_blocked_below_margin():
    # Fresh 60 - weak 50 = 10 < 15 required.
    actions, _ = _rotation_candidates(_state(), _watchlist(60.0), POSITIONS)
    assert actions == []


def test_blocked_under_min_hold():
    # Weak holding only 1 day old (< 3-day swing min hold).
    actions, _ = _rotation_candidates(_state(weak_age_days=1), _watchlist(80.0), POSITIONS)
    assert actions == []


def test_blocked_at_churn_cap():
    today = datetime.now(timezone.utc).date().isoformat()
    actions, _ = _rotation_candidates(
        _state(rotations_today={"date": today, "swing": ROTATION_DAILY_CAP}), _watchlist(80.0), POSITIONS)
    assert actions == []


def test_lottery_never_rotates():
    log = [
        {"side": "buy", "ticker": "L1", "bucket": "lottery", "ts": _now_minus(30)},
        {"side": "buy", "ticker": "L2", "bucket": "lottery", "ts": _now_minus(30)},
    ]
    positions = [
        {"ticker": "L1", "bucket": "lottery", "shares": 1.0, "pl_dollars": 0.0},
        {"ticker": "L2", "bucket": "lottery", "shares": 1.0, "pl_dollars": 0.0},
    ]
    watch = [
        {"ticker": "LX", "bucket": "lottery", "score": 99.0},
        {"ticker": "L1", "bucket": "lottery", "score": 10.0},
    ]
    actions, _ = _rotation_candidates({"order_log": log}, watch, positions)
    assert actions == []


def run():
    test_fires_when_all_conditions_met()
    test_blocked_below_margin()
    test_blocked_under_min_hold()
    test_blocked_at_churn_cap()
    test_lottery_never_rotates()
