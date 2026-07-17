"""Offline tests for the correlated-theme cap (max 1 name per theme per bucket)."""
from agents._screener import THEME_OF
from agents.allocator.agent import _pick_candidates, _rotation_candidates, _themes_held


def _cand(ticker, score=60.0, price=10.0):
    return {"ticker": ticker, "bucket": "lottery", "score": score, "last_price": price}


def _price(c):
    return c.get("last_price")


def test_theme_of_covers_miners():
    assert {THEME_OF[t] for t in ("MARA", "RIOT", "WULF", "CLSK")} == {"crypto"}


def test_pick_skips_second_name_of_same_theme():
    # MARA and RIOT are both crypto; only the higher-ranked one is picked and
    # the unthemed name fills the second slot.
    cands = [_cand("MARA", 70), _cand("RIOT", 65), _cand("OPEN", 60)]
    picked = _pick_candidates(cands, 2, set(), _price)
    assert [c["ticker"] for c, _ in picked] == ["MARA", "OPEN"]


def test_pick_respects_already_held_theme():
    # A crypto name already held (used_themes seeded) blocks ALL crypto candidates.
    cands = [_cand("MARA", 70), _cand("RIOT", 65), _cand("OPEN", 60)]
    used = {"crypto"}
    picked = _pick_candidates(cands, 2, used, _price)
    assert [c["ticker"] for c, _ in picked] == ["OPEN"]
    assert "crypto" in used


def test_pick_unthemed_unconstrained():
    cands = [_cand("AAA", 70), _cand("BBB", 65)]
    picked = _pick_candidates(cands, 2, set(), _price)
    assert len(picked) == 2


def test_pick_unpriceable_falls_through():
    # Price miss on the top name falls through to the next-best (6/23 fix kept).
    cands = [_cand("AAA", 70, price=None), _cand("BBB", 65)]
    picked = _pick_candidates(cands, 1, set(), _price)
    assert [c["ticker"] for c, _ in picked] == ["BBB"]


def test_themes_held():
    assert _themes_held(["MARA", "NOW", "IONQ"]) == {"crypto", "quantum"}
    assert _themes_held(["JPM"]) == set()


def test_rotation_blocked_by_theme():
    # Swing bucket full with COIN (crypto) + a weak unthemed name. The best fresh
    # candidate HOOD is also crypto: rotating the unthemed weak name out would
    # leave COIN + HOOD stacked, so the rotation must not fire.
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    state = {"order_log": [
        {"side": "buy", "ticker": "COIN", "bucket": "swing", "ts": old},
        {"side": "buy", "ticker": "WEAK", "bucket": "swing", "ts": old},
    ]}
    positions = [
        {"ticker": "COIN", "bucket": "swing", "shares": 1.0, "pl_dollars": 0.0},
        {"ticker": "WEAK", "bucket": "swing", "shares": 1.0, "pl_dollars": -0.5},
    ]
    watch = [
        {"ticker": "HOOD", "bucket": "swing", "score": 90.0},
        {"ticker": "COIN", "bucket": "swing", "score": 60.0},
        {"ticker": "WEAK", "bucket": "swing", "score": 40.0},
    ]
    actions, _ = _rotation_candidates(state, watch, positions)
    assert actions == []


def run():
    test_theme_of_covers_miners()
    test_pick_skips_second_name_of_same_theme()
    test_pick_respects_already_held_theme()
    test_pick_unthemed_unconstrained()
    test_pick_unpriceable_falls_through()
    test_themes_held()
    test_rotation_blocked_by_theme()
