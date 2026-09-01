"""Offline tests for the entry-extension filter (research) and the ATR risk cap
(allocator sizing). Pure functions, no network."""
from agents.allocator.agent import RISK_PER_TRADE_FRAC, _atr_risk_cap
from agents.research.agent import _drop_extended


def _c(bucket, above=None, rsi=None):
    c = {"ticker": "TST", "bucket": bucket}
    if above is not None:
        c["above_ma50"] = above
    if rsi is not None:
        c["rsi14"] = rsi
    return c


def test_extended_core_dropped():
    # Core cap is 12% above 50DMA — 13% is chasing.
    assert _drop_extended([_c("core", above=13.0, rsi=60.0)]) == []


def test_overbought_dropped():
    assert _drop_extended([_c("swing", above=5.0, rsi=80.0)]) == []


def test_normal_passes():
    kept = _drop_extended([_c("core", above=6.0, rsi=55.0)])
    assert len(kept) == 1


def test_missing_fields_fail_open():
    # Stale watchlist without the new fields must pass through (fail-open,
    # mirroring the earnings filter's stance on unknown data).
    kept = _drop_extended([_c("core")])
    assert len(kept) == 1


def test_lottery_wide_band():
    # 25% above MA is fine for lottery (cap 30) but way past core's 12.
    assert len(_drop_extended([_c("lottery", above=25.0, rsi=60.0)])) == 1
    assert _drop_extended([_c("core", above=25.0, rsi=60.0)]) == []


def test_atr_risk_cap_scales_inverse_to_atr():
    # equity 300, risk 1% = $3. ATR 2% -> stop 5% -> cap $60. ATR 4% -> 10% -> $30.
    calm = _atr_risk_cap({"atr_pct": 2.0}, 300.0, "core")
    wild = _atr_risk_cap({"atr_pct": 4.0}, 300.0, "core")
    assert round(calm, 2) == round(RISK_PER_TRADE_FRAC * 300.0 / 0.05, 2) == 60.0
    assert round(wild, 2) == 30.0
    assert calm > wild


def test_atr_risk_cap_uses_effective_stop():
    # ATR 10% on core: stop distance clamps at the -15 bucket floor, not 25%.
    cap = _atr_risk_cap({"atr_pct": 10.0}, 300.0, "core")
    assert round(cap, 2) == round(3.0 / 0.15, 2) == 20.0


def test_atr_risk_cap_absent_without_atr():
    # Legacy candidates without atr_pct -> no cap (prior sizing unchanged).
    assert _atr_risk_cap({}, 300.0, "core") is None
    assert _atr_risk_cap({"atr_pct": 2.0}, 0.0, "core") is None


def run():
    test_extended_core_dropped()
    test_overbought_dropped()
    test_normal_passes()
    test_missing_fields_fail_open()
    test_lottery_wide_band()
    test_atr_risk_cap_scales_inverse_to_atr()
    test_atr_risk_cap_uses_effective_stop()
    test_atr_risk_cap_absent_without_atr()
