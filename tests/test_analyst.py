"""Offline tests for the Claude analyst tilt (pure application logic — the
actual API call is never made here)."""
import os

from agents import _analyst
from agents._analyst import VIEW_POINTS, apply_views, enrich_with_analyst


def _cands():
    return [
        {"ticker": "AAA", "bucket": "swing", "score": 60.0, "conviction": 4, "thesis": "quant"},
        {"ticker": "BBB", "bucket": "swing", "score": 58.0, "conviction": 4, "thesis": "quant"},
        {"ticker": "CCC", "bucket": "core", "score": 55.0, "conviction": 3, "thesis": "quant"},
    ]


def test_views_tilt_and_rerank():
    verdicts = {
        "AAA": {"ticker": "AAA", "view": -2, "note": "SEC investigation announced"},
        "BBB": {"ticker": "BBB", "view": 2, "note": "raised guidance on real demand"},
    }
    out = apply_views(_cands(), verdicts)
    by = {c["ticker"]: c for c in out}
    assert by["AAA"]["score"] == 60.0 - 2 * VIEW_POINTS
    assert by["BBB"]["score"] == 58.0 + 2 * VIEW_POINTS
    assert by["CCC"]["score"] == 55.0            # no verdict -> untouched
    assert out[0]["ticker"] == "BBB"             # re-ranked by tilted score
    assert "analyst: SEC investigation" in by["AAA"]["thesis"]


def test_neutral_and_clamped_views():
    verdicts = {
        "AAA": {"ticker": "AAA", "view": 0, "note": "no headlines"},
        "BBB": {"ticker": "BBB", "view": 9, "note": "out-of-range gets clamped"},
    }
    out = apply_views(_cands(), verdicts)
    by = {c["ticker"]: c for c in out}
    assert by["AAA"]["score"] == 60.0            # view 0 -> no score change
    assert by["BBB"]["score"] == 58.0 + 2 * VIEW_POINTS  # clamped to +2


def test_degrades_without_api_key():
    key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        cands = _cands()
        assert enrich_with_analyst(cands) is cands  # untouched pass-through
    finally:
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key


def test_degrades_on_model_failure():
    def _boom(payload):
        raise RuntimeError("api down")
    orig_ask, orig_news = _analyst._ask_claude, _analyst._recent_headlines
    _analyst._ask_claude = _boom
    _analyst._recent_headlines = lambda t: {}
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    try:
        cands = _cands()
        out = enrich_with_analyst(cands)
        assert [c["score"] for c in out] == [60.0, 58.0, 55.0]  # no-op
    finally:
        _analyst._ask_claude, _analyst._recent_headlines = orig_ask, orig_news


def run():
    test_views_tilt_and_rerank()
    test_neutral_and_clamped_views()
    test_degrades_without_api_key()
    test_degrades_on_model_failure()
