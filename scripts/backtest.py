"""Walk-forward backtest: does the NEW core strategy beat the OLD one and SPY?

Compares, on the CORE bucket only (80% of capital — the meaningful sleeve):
  NEW  = z-scored weighted factor scoring + inverse-vol sizing + trailing/stop/trend exits
  OLD  = raw momentum sum (above_MA + ret10 + RS) + equal weight + -15% stop only
  SPY  = buy & hold benchmark

Daily close resolution, weekly rebalance. No fees/slippage modeled (Alpaca is
commission-free + fractional). Past performance != future — this is a sanity
check on the logic change, not a promise. Run:  .venv/bin/python scripts/backtest.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yfinance as yf

from agents._regime import exposure_from_signals
from agents._screener import CORE_UNIVERSE, FACTOR_WEIGHTS, _zscore, fetch_history, raw_factors
from agents.allocator.agent import _inverse_vol_budgets

LOOKBACK_DAYS = 500
REBAL_EVERY = 5          # trading days (~weekly)
TOP_N = 5
WARMUP = 60              # need >=50 sessions for MA / Sharpe
HARD_STOP = -15.0        # both strategies keep the -15 floor
TRAIL_ARM, TRAIL = 8.0, 12.0  # NEW core trailing stop
TREND_BAND = -0.02       # NEW core trend-break vs 50DMA


def _old_score(close: pd.Series, spy_close: pd.Series) -> float:
    if len(close) < 50:
        return -999.0
    ma50 = close.rolling(50).mean().iloc[-1]
    ret10 = (close.iloc[-1] / close.iloc[-11] - 1) * 100 if len(close) > 11 else 0.0
    above = (close.iloc[-1] / ma50 - 1) * 100 if ma50 else 0.0
    spy_ret10 = (spy_close.iloc[-1] / spy_close.iloc[-11] - 1) * 100 if len(spy_close) > 11 else 0.0
    return float(above + ret10 + (ret10 - spy_ret10))


def _rank(strategy, hist, tickers, upto, spy_close):
    """Return [(ticker, vol20)] of the top picks for the strategy at day `upto`."""
    if strategy == "old":
        out = []
        for t in tickers:
            close = hist[t]["Close"].iloc[:upto + 1].dropna()
            if len(close) < 50:
                continue
            out.append((t, _old_score(close, spy_close.iloc[:upto + 1]), 0.02))
        ranked = sorted(out, key=lambda x: x[1], reverse=True)
        return [(t, v) for (t, s, v) in ranked[:TOP_N]]
    # new
    spy_ret10 = (spy_close.iloc[upto] / spy_close.iloc[upto - 10] - 1) * 100 if upto > 10 else 0.0
    rows = {}
    for t in tickers:
        df = hist[t].iloc[:upto + 1]
        if len(df) < 50 or pd.isna(df["Close"].iloc[-1]):
            continue
        rows[t] = raw_factors(df, spy_ret10)
    if len(rows) < 3:
        return []
    fdf = pd.DataFrame(rows).T
    w = FACTOR_WEIGHTS["core"]
    comp = sum(w[f] * _zscore(fdf[f]) for f in w)
    scores = 50 + 10 * comp
    ranked = sorted(((t, float(scores[t]), float(fdf.loc[t, "vol20"])) for t in fdf.index),
                    key=lambda x: x[1], reverse=True)
    return [(t, v) for (t, s, v) in ranked[:TOP_N]]


def _exited(strategy, hist, ticker, i, entry, peak_px) -> bool:
    px = hist[ticker]["Close"].iloc[i]
    pl = (px / entry - 1) * 100
    if pl <= HARD_STOP:
        return True
    if strategy != "new":
        return False
    peak_pl = (peak_px / entry - 1) * 100
    if peak_pl >= TRAIL_ARM and pl <= peak_pl - TRAIL:
        return True
    close = hist[ticker]["Close"].iloc[:i + 1]
    ma50 = close.rolling(50).mean().iloc[-1]
    return bool(ma50 and (px / ma50 - 1) < TREND_BAND)


def _breadth(hist, tickers, i) -> float:
    n = ok = 0
    for t in tickers:
        c = hist[t]["Close"].iloc[:i + 1].dropna()
        if len(c) < 50:
            continue
        n += 1
        if c.iloc[-1] > c.rolling(50).mean().iloc[-1]:
            ok += 1
    return ok / n if n else 0.5


def _simulate(strategy, hist, dates, spy_close, tickers, regime=False, vix=None) -> list[float]:
    cash, sleeves, curve = 1.0, [], []
    for i in range(WARMUP, len(dates)):
        # 1. mark-to-market + exits (skip the first day — no prior close yet)
        if i > WARMUP:
            survivors = []
            for s in sleeves:
                px, ppx = hist[s["ticker"]]["Close"].iloc[i], hist[s["ticker"]]["Close"].iloc[i - 1]
                if pd.isna(px) or pd.isna(ppx) or ppx == 0:
                    survivors.append(s)
                    continue
                s["value"] *= px / ppx
                s["peak"] = max(s["peak"], px)
                if _exited(strategy, hist, s["ticker"], i, s["entry"], s["peak"]):
                    cash += s["value"]          # exit to cash until next rebalance
                else:
                    survivors.append(s)
            sleeves = survivors
        # 2. weekly rebalance
        if (i - WARMUP) % REBAL_EVERY == 0:
            total = cash + sum(s["value"] for s in sleeves)
            # regime filter scales how much we deploy (rest stays cash)
            exposure = 1.0
            if regime:
                v = float(vix.iloc[i]) if (vix is not None and not pd.isna(vix.iloc[i])) else None
                exposure, _ = exposure_from_signals(spy_close.iloc[:i + 1], _breadth(hist, tickers, i), v)
            deploy = total * exposure
            picks = _rank(strategy, hist, tickers, i, spy_close)
            if picks:
                if strategy == "new":
                    budgets = _inverse_vol_budgets(
                        [{"ticker": t, "vol20": v} for (t, v) in picks], deploy, "core")
                else:
                    budgets = [deploy / len(picks)] * len(picks)
                sleeves, spent = [], 0.0
                for (t, _v), b in zip(picks, budgets):
                    px = hist[t]["Close"].iloc[i]
                    if pd.isna(px) or px <= 0:
                        continue
                    sleeves.append({"ticker": t, "value": b, "entry": px, "peak": px})
                    spent += b
                cash = total - spent
        curve.append(cash + sum(s["value"] for s in sleeves))
    return curve


def _metrics(curve: list[float]) -> dict:
    eq = np.array(curve)
    rets = np.diff(eq) / eq[:-1]
    peak = np.maximum.accumulate(eq)
    return {
        "total_return_pct": (eq[-1] / eq[0] - 1) * 100,
        "max_drawdown_pct": ((eq - peak) / peak).min() * 100,
        "sharpe": (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 1e-9 else 0.0,
    }


def main():
    print(f"Fetching ~{LOOKBACK_DAYS}d history for {len(CORE_UNIVERSE)} core tickers...")
    hist = fetch_history(CORE_UNIVERSE, days=LOOKBACK_DAYS)
    if "SPY" not in hist:
        print("No SPY data — aborting."); return
    # Align everything to SPY's trading calendar.
    dates = hist["SPY"].index
    tickers = [t for t in CORE_UNIVERSE if t in hist and t != "SPY"]
    for t in tickers + ["SPY"]:
        hist[t] = hist[t].reindex(dates).ffill()
    spy_close = hist["SPY"]["Close"]
    try:
        vix = yf.download("^VIX", period="2y", progress=False, auto_adjust=True)["Close"].reindex(dates).ffill()
        if isinstance(vix, pd.DataFrame):
            vix = vix.iloc[:, 0]
    except Exception:
        vix = None

    new_curve = _simulate("new", hist, dates, spy_close, tickers)
    reg_curve = _simulate("new", hist, dates, spy_close, tickers, regime=True, vix=vix)
    old_curve = _simulate("old", hist, dates, spy_close, tickers)
    spy_curve = list((spy_close.iloc[WARMUP:] / spy_close.iloc[WARMUP]).values)

    span = (dates[-1] - dates[WARMUP]).days
    print(f"\nBacktest window: {dates[WARMUP].date()} -> {dates[-1].date()}  ({span} days, {len(tickers)} tickers)\n")
    print(f"{'strategy':<12} {'total ret':>10} {'max DD':>9} {'Sharpe':>8}")
    print("-" * 42)
    for name, curve in (("NEW", new_curve), ("NEW+REGIME", reg_curve), ("OLD", old_curve), ("SPY", spy_curve)):
        m = _metrics(curve)
        print(f"{name:<12} {m['total_return_pct']:>9.1f}% {m['max_drawdown_pct']:>8.1f}% {m['sharpe']:>8.2f}")
    print("\nCaveats: daily-close resolution, weekly rebalance, core bucket only, no "
          "fees/slippage, single historical path. Indicative only — not a forecast.")


if __name__ == "__main__":
    main()
