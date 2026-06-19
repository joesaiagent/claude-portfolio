"""Offline tests for the regime exposure mapping (pure function)."""
import pandas as pd

from agents._regime import EXPOSURE, breadth_above_ma, exposure_from_signals


def _spy(trend: str, n: int = 220) -> pd.Series:
    step = {"up": 1.0, "down": -1.0, "flat": 0.0}[trend]
    return pd.Series([400 + step * i for i in range(n)])


def test_risk_on_strong_uptrend():
    exp, label = exposure_from_signals(_spy("up"), breadth_pct=0.8, vix=14)
    assert label == "risk_on" and exp == EXPOSURE["risk_on"]


def test_risk_off_downtrend_high_vix():
    exp, label = exposure_from_signals(_spy("down"), breadth_pct=0.2, vix=35)
    assert label == "risk_off" and exp == EXPOSURE["risk_off"]
    assert exp < EXPOSURE["risk_on"]  # deploys less in a selloff


def test_neutral_mixed():
    # Up vs 200/50 (+2) but weak breadth (-1) and high vix (-1) -> net 0 -> neutral.
    exp, label = exposure_from_signals(_spy("up"), breadth_pct=0.35, vix=30)
    assert label == "neutral"


def test_breadth_calc():
    closes = {
        "A": pd.Series([1 + 0.1 * i for i in range(60)]),   # above its MA
        "B": pd.Series([100 - i for i in range(60)]),        # below its MA
    }
    b = breadth_above_ma(closes, window=50)
    assert 0.0 <= b <= 1.0 and abs(b - 0.5) < 1e-9


def run():
    test_risk_on_strong_uptrend()
    test_risk_off_downtrend_high_vix()
    test_neutral_mixed()
    test_breadth_calc()
