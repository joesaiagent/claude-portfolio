"""Market regime filter — the strategy's 'defense' gear.

Turns broad market signals (SPY trend, breadth, volatility) into an EXPOSURE
multiplier that scales how much capital the allocator deploys. In a downtrend it
shrinks bucket targets so the book holds cash instead of buying momentum into a
selloff — the single biggest lever for cutting drawdown.

`exposure_from_signals` is a pure function (unit-tested). `current_regime`
wires it to live data; backtest computes the same signals on historical slices.
"""
import pandas as pd
import yfinance as yf

from agents._screener import CORE_UNIVERSE, fetch_history

# Exposure multipliers applied to every bucket's target dollars. Tuned "bear-only":
# full exposure in uptrends AND ordinary chop (don't bleed return on shallow dips),
# cut to half only in a genuine risk_off (deep bear) — backtests showed this keeps
# ~96% of the upside while cutting ~1/3 of the worst drawdown.
EXPOSURE = {"risk_on": 1.0, "neutral": 1.0, "risk_off": 0.5}


def exposure_from_signals(spy_close: pd.Series, breadth_pct: float, vix: float | None) -> tuple[float, str]:
    """Map market signals -> (exposure multiplier, label). Pure / testable.

    Score components (each -1/0/+1): SPY vs 200DMA, SPY vs 50DMA, breadth
    (% of universe above its 50DMA), VIX level. Sum >=2 risk_on, <=-2 risk_off.
    """
    score = 0
    if len(spy_close) >= 50:
        last = spy_close.iloc[-1]
        ma200 = spy_close.rolling(200).mean().iloc[-1] if len(spy_close) >= 200 else spy_close.mean()
        ma50 = spy_close.rolling(50).mean().iloc[-1]
        score += 1 if last > ma200 else -1
        score += 1 if last > ma50 else -1
    if breadth_pct >= 0.60:
        score += 1
    elif breadth_pct <= 0.40:
        score -= 1
    if vix is not None:
        if vix >= 28:
            score -= 1
        elif vix <= 18:
            score += 1
    label = "risk_on" if score >= 2 else "risk_off" if score <= -2 else "neutral"
    return EXPOSURE[label], label


def breadth_above_ma(closes: dict[str, pd.Series], window: int = 50) -> float:
    """Fraction of tickers trading above their `window`-day moving average."""
    n = ok = 0
    for s in closes.values():
        s = s.dropna()
        if len(s) < window:
            continue
        n += 1
        if s.iloc[-1] > s.rolling(window).mean().iloc[-1]:
            ok += 1
    return ok / n if n else 0.5


def _latest_vix() -> float | None:
    try:
        df = yf.download("^VIX", period="2mo", progress=False, auto_adjust=True)
        return float(df["Close"].iloc[-1]) if not df.empty else None
    except Exception:
        return None


def current_regime() -> tuple[float, str, dict]:
    """Live regime read. Falls back to full exposure if data is unavailable
    (never let a data outage force the book into cash erroneously)."""
    hist = fetch_history(CORE_UNIVERSE, days=300)
    spy = hist.get("SPY")
    if spy is None or spy.empty:
        return 1.0, "unknown", {"reason": "no SPY data"}
    closes = {t: hist[t]["Close"] for t in hist if t != "SPY"}
    breadth = breadth_above_ma(closes)
    vix = _latest_vix()
    exposure, label = exposure_from_signals(spy["Close"], breadth, vix)
    return exposure, label, {"breadth": round(breadth, 2), "vix": vix}
