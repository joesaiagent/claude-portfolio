"""Offline tests for signal attribution: legacy thesis-string parsing,
structured-snapshot preference, and the scipy-free Spearman. No network."""
from agents.attribution import _spearman, parse_signals


LEGACY = ("CRWD: momentum 60, news +0.20 (50), insider txn +0.00, "
          "congress +0.00, options PCR +0.08, analyst -7% (rec 1.7) "
          "| analyst: upgrades signal confidence.")


def test_parse_legacy_thesis():
    sig = parse_signals({"rationale": LEGACY})
    assert sig["momentum_score"] == 60.0
    assert sig["news_sentiment"] == 0.20
    assert sig["insider_transactions"] == 0.0
    assert sig["options_pcr_signal"] == 0.08
    assert sig["analyst_upside_pct"] == -7.0
    assert sig["analyst_rec"] == 1.7


def test_structured_snapshot_preferred():
    # When the allocator stamped `signals`, use it verbatim — no regex.
    e = {"rationale": LEGACY, "signals": {"score": 61.5, "rsi14": 62.0, "junk": "x"}}
    sig = parse_signals(e)
    assert sig == {"score": 61.5, "rsi14": 62.0}


def test_unparseable_returns_empty():
    assert parse_signals({"rationale": "pyramid: adding to winner at +20.0%"}) == {}
    assert parse_signals({}) == {}


def test_spearman_monotonic():
    assert abs(_spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9
    assert abs(_spearman([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-9


def test_spearman_degenerate():
    # Constant signal (the congressional stub) carries no information.
    assert _spearman([0, 0, 0, 0], [1, 2, 3, 4]) is None
    assert _spearman([1, 2], [1, 2]) is None  # too few points


def run():
    test_parse_legacy_thesis()
    test_structured_snapshot_preferred()
    test_unparseable_returns_empty()
    test_spearman_monotonic()
    test_spearman_degenerate()
