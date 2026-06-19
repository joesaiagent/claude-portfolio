"""Deterministic, offline tests for the rebuilt screener scoring."""
import pandas as pd

from agents import _screener
from agents._screener import CORE_UNIVERSE, _zscore, raw_factors, screen_bucket


def _df(n: int, start: float, step: float, vol_step: int = 1000) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Close": [start + step * i for i in range(n)],
         "Volume": [1_000_000 + vol_step * i for i in range(n)]},
        index=idx,
    )


def test_zscore():
    # Zero variance -> all zeros (no div-by-zero blowup).
    flat = pd.Series([5.0, 5.0, 5.0])
    assert (_zscore(flat) == 0.0).all()
    # Known spread: mean removed, scaled by population std.
    z = _zscore(pd.Series([1.0, 2.0, 3.0]))
    assert abs(z.mean()) < 1e-9
    assert z.iloc[2] > z.iloc[0]


def test_raw_factors_signs():
    up = raw_factors(_df(60, 100, 1.0), spy_ret10=0.0)
    assert up["ret5"] > 0 and up["ret10"] > 0 and up["above_ma50"] > 0
    assert up["sharpe"] > 0
    down = raw_factors(_df(60, 100, -0.5), spy_ret10=0.0)
    assert down["ret5"] < 0 and down["sharpe"] < 0
    assert up["vol20"] >= 0


def test_screen_bucket_schema_and_ranking(monkeypatch):
    t_up, t_flat, t_down = CORE_UNIVERSE[0], CORE_UNIVERSE[1], CORE_UNIVERSE[2]
    histories = {
        t_up: _df(60, 100, 1.2, vol_step=5000),     # strong uptrend, rising volume
        t_flat: _df(60, 100, 0.0, vol_step=0),       # flat
        t_down: _df(60, 100, -0.8, vol_step=-2000),  # downtrend, fading volume
        "SPY": _df(60, 400, 0.2),                    # mild market uptrend
    }
    monkeypatch.setattr(_screener, "fetch_history", lambda tickers, days=90: histories)

    # SPY is itself a CORE_UNIVERSE member (a tradeable index ETF), so it scores
    # as a candidate too — expect our 3 names plus SPY.
    out = screen_bucket("core", top_n=5)
    required = {"ticker", "bucket", "score", "last_price", "vol20",
                "sharpe", "vol_confirm", "conviction", "thesis"}
    for c in out:
        assert required <= set(c), f"missing keys: {required - set(c)}"
        assert 1 <= c["conviction"] <= 5
    # Sorted by score descending.
    scores = [c["score"] for c in out]
    assert scores == sorted(scores, reverse=True)
    d = {c["ticker"]: c for c in out}
    # Uptrend clearly beats downtrend; conviction monotonic in score.
    assert d[t_up]["score"] > d[t_down]["score"]
    assert d[t_up]["conviction"] >= d[t_down]["conviction"]


# Minimal monkeypatch shim so we don't depend on pytest.
class _MP:
    def __init__(self): self._undo = []
    def setattr(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
    def undo(self):
        for obj, name, val in reversed(self._undo):
            setattr(obj, name, val)
        self._undo.clear()


def run():
    test_zscore()
    test_raw_factors_signs()
    mp = _MP()
    try:
        test_screen_bucket_schema_and_ranking(mp)
    finally:
        mp.undo()
