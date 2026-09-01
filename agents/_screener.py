"""Free-tier screening helpers — Alpaca bars + Python rules, no LLM.
yfinance is the FALLBACK price source only (it caused the 6/22 spurious sell,
the tz-cache incident, and repeated flaky-fetch warnings); Alpaca's data API is
keyed anyway, first-party, and returns split/dividend-adjusted daily bars."""
from __future__ import annotations

import os
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)


# Curated universes — tweakable, no API cost to scan. Tech-forward by request;
# NO healthcare/med names (also enforced live by the sector filter in _intel).
CORE_UNIVERSE = [
    # mega/large-cap tech core
    "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "AVGO", "ORCL", "AMD", "MU",
    "NOW", "CRM", "ADBE", "ACN", "CSCO", "QCOM", "TXN", "INTU", "PANW", "AMAT",
    # non-health diversifiers (financials/consumer/auto) to avoid single-sector risk
    "JPM", "V", "MA", "COST", "WMT", "HD", "TSLA",
    # tech-tilted ETFs + benchmarks (SPY/QQQ also used for relative strength)
    "QQQ", "SPY", "SMH", "XLK", "SCHG", "VOOG",
]

SWING_UNIVERSE = [
    "NVDA", "AMD", "MU", "PLTR", "SOFI", "RBLX", "COIN", "HOOD", "NFLX",
    "UBER", "ABNB", "SHOP", "CRWD", "SNOW", "DDOG", "ZS", "NET", "MDB", "OKTA",
]

LOTTERY_UNIVERSE = [
    # AI / quantum / space / crypto-mining asymmetric names (no cannabis/med)
    "SOUN", "BBAI", "RGTI", "QBTS", "IONQ", "ACHR", "JOBY", "ASTS", "RKLB",
    "OPEN", "MARA", "RIOT", "CLSK", "WULF",
]

# Sectors we never hold, enforced live regardless of universe edits.
EXCLUDED_SECTORS = {"Healthcare"}

# Correlated theme clusters. The allocator holds at most ONE name per theme per
# bucket, so a momentum run in a single theme can't stack a bucket into one
# correlated bet — on 2026-07-16 the lottery book was WULF+RIOT+MARA, three BTC
# miners that drew down -23..-34% together as one trade. Names outside any
# cluster are unconstrained. Add clusters here as universes evolve.
THEMES = {
    "crypto": {"MARA", "RIOT", "CLSK", "WULF", "COIN", "HOOD"},
    "quantum": {"RGTI", "QBTS", "IONQ"},
    "space": {"ACHR", "JOBY", "ASTS", "RKLB"},
    "ai-smallcap": {"SOUN", "BBAI"},
}

# Flat ticker -> theme lookup derived from THEMES.
THEME_OF = {t: theme for theme, members in THEMES.items() for t in members}


def _alpaca_history(tickers: list[str], days: int) -> dict[str, pd.DataFrame]:
    """Daily OHLCV from Alpaca's data API (one batched request, ALL-adjusted so
    prices match yfinance's auto_adjust). Frames use yfinance's column names and
    a tz-naive midnight index so every downstream consumer (factors, MAs,
    backtest reindexing against yf's VIX) sees the same shape either way."""
    client = _bars_client()
    req = StockBarsRequest(
        symbol_or_symbols=list(tickers),
        timeframe=TimeFrame.Day,
        start=datetime.now() - timedelta(days=days),
        adjustment=Adjustment.ALL,
    )
    data = client.get_stock_bars(req).df
    out: dict[str, pd.DataFrame] = {}
    if data is None or data.empty:
        return out
    for t in tickers:
        try:
            df = data.loc[t]
        except KeyError:
            continue  # symbol Alpaca doesn't cover — yfinance fallback picks it up
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
        idx = pd.DatetimeIndex(df.index).tz_convert("America/New_York").tz_localize(None).normalize()
        df = df[["Open", "High", "Low", "Close", "Volume"]].set_axis(idx, axis=0).dropna()
        if len(df) >= 30:
            out[t] = df
    return out


def _bars_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    )


try:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment
except Exception:  # pragma: no cover — alpaca-py is a hard dep, but stay importable
    StockBarsRequest = None


def fetch_history(tickers: list[str], days: int = 90) -> dict[str, pd.DataFrame]:
    """Pull OHLCV history for a list of tickers. Returns {ticker: dataframe}.
    Alpaca first; any tickers it can't serve (or a full outage) fall back to
    yfinance, so one degraded source can't blind the screeners/regime/exits."""
    out: dict[str, pd.DataFrame] = {}
    if StockBarsRequest is not None:
        try:
            out = _alpaca_history(tickers, days)
        except Exception as e:
            print(f"[screener] alpaca bars failed ({str(e)[:120]}); falling back to yfinance")
    missing = [t for t in tickers if t not in out]
    if not missing:
        return out

    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        data = yf.download(
            missing, start=start, end=end, group_by="ticker", auto_adjust=True,
            progress=False, threads=True,
        )
    except Exception:
        return out

    for t in missing:
        try:
            df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            if df.empty or "Close" not in df.columns:
                continue
            df = df.dropna()
            if len(df) < 30:
                continue
            out[t] = df
        except Exception:
            continue
    return out


# Per-bucket factor weights (each set sums to 1.0). Scores are computed by
# z-scoring each factor ACROSS the bucket universe, then weighting — so factors
# on different scales (returns vs Sharpe vs volume) become comparable. Volatility
# deliberately does NOT select here (it only sizes, in the allocator); lottery
# now requires real volume + trend instead of just "most pumped + most volatile".
FACTOR_WEIGHTS = {
    "core":    {"ret10": 0.20, "above_ma50": 0.20, "rs10": 0.25, "sharpe": 0.25, "vol_confirm": 0.10},
    "swing":   {"ret5": 0.30, "rs10": 0.15, "sharpe": 0.20, "vol_confirm": 0.25, "vol_expansion": 0.10},
    "lottery": {"ret5": 0.30, "sharpe": 0.15, "vol_confirm": 0.30, "above_ma50": 0.10, "rs10": 0.15},
}


def _zscore(s: pd.Series) -> pd.Series:
    """Cross-sectional z-score. Zero/degenerate variance -> all zeros."""
    s = pd.to_numeric(s, errors="coerce").fillna(0.0)
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd < 1e-9:
        return s * 0.0
    return (s - s.mean()) / sd


def _finite(x: float, default: float = 0.0) -> float:
    return float(x) if x is not None and np.isfinite(x) else default


def atr_pct_from_history(df: pd.DataFrame, period: int = 14) -> float:
    """14-day ATR as a PERCENT of the last close (sizing/stops both want the
    relative number, not dollars). True range uses High/Low when present;
    degrades to close-to-close absolute moves when a source omits them.
    Falls back to 2.0 (a middling large-cap ATR%) if there's no usable data,
    so callers can rely on a sane positive value."""
    close = df["Close"].dropna()
    if len(close) < period + 1:
        return 2.0
    if "High" in df.columns and "Low" in df.columns and df["High"].notna().all():
        prev_close = close.shift(1)
        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
    else:
        tr = close.diff().abs()
    atr = tr.tail(period).mean()
    last = float(close.iloc[-1])
    if not last or not np.isfinite(atr) or atr <= 0:
        return 2.0
    return float(atr / last * 100)


def rsi14(close: pd.Series, period: int = 14) -> float:
    """Wilder RSI. Neutral 50.0 on insufficient/degenerate data."""
    close = close.dropna()
    if len(close) < period + 1:
        return 50.0
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    if not np.isfinite(gain) or not np.isfinite(loss):
        return 50.0
    if loss < 1e-12:
        return 100.0
    rs = gain / loss
    return float(100 - 100 / (1 + rs))


def raw_factors(df: pd.DataFrame, spy_ret10: float) -> dict:
    """Raw (un-normalized) factor values for one ticker. Normalization happens
    cross-sectionally in screen_bucket. Returns include vol20 (for sizing)."""
    close = df["Close"].dropna()
    n = len(close)
    last = float(close.iloc[-1])
    ret5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if n > 6 else 0.0
    ret10 = (close.iloc[-1] / close.iloc[-11] - 1) * 100 if n > 11 else 0.0
    ma50 = close.rolling(50).mean().iloc[-1] if n >= 50 else close.mean()
    above_ma50 = (last / ma50 - 1) * 100 if ma50 else 0.0
    dret = close.pct_change().dropna()
    sd20 = dret.tail(20).std(ddof=0)
    sharpe = (dret.tail(20).mean() / sd20) if sd20 and sd20 > 1e-9 else 0.0
    vol20 = float(sd20) if sd20 and np.isfinite(sd20) else 0.02
    vshort = dret.tail(5).std(ddof=0)
    vlong = dret.tail(30).std(ddof=0)
    vol_expansion = (vshort / vlong - 1) * 100 if vlong and vlong > 1e-9 else 0.0
    vol_confirm = 0.0
    if "Volume" in df.columns:
        v5 = df["Volume"].tail(5).mean()
        v20 = df["Volume"].tail(20).mean()
        vol_confirm = (v5 / v20 - 1) * 100 if v20 and v20 > 0 and np.isfinite(v20) else 0.0
    return {
        "last_price": round(last, 2),
        "ret5": _finite(ret5), "ret10": _finite(ret10), "above_ma50": _finite(above_ma50),
        "rs10": _finite(ret10 - spy_ret10), "sharpe": _finite(sharpe),
        "vol_confirm": _finite(vol_confirm), "vol_expansion": _finite(vol_expansion),
        "vol20": round(vol20, 4),
        # Not scoring factors — carried through for entry filters (rsi14, above_ma50)
        # and ATR-risk sizing/stops (atr_pct) downstream.
        "atr_pct": round(atr_pct_from_history(df), 3),
        "rsi14": round(rsi14(close), 2),
    }


def screen_bucket(bucket: str, top_n: int = 5) -> list[dict]:
    """Return ranked candidates for a bucket via normalized, weighted factor scoring.
    No LLM calls. Output schema is back-compatible (ticker/bucket/score/last_price/
    conviction/thesis) plus additive fields vol20/sharpe/vol_confirm used downstream."""
    universe_map = {
        "core": CORE_UNIVERSE,
        "swing": SWING_UNIVERSE,
        "lottery": LOTTERY_UNIVERSE,
    }
    universe = universe_map[bucket]
    histories = fetch_history(universe + ["SPY"], days=90)  # SPY for RS in every bucket
    spy = histories.get("SPY")
    spy_ret10 = 0.0
    if spy is not None and len(spy) > 11:
        spy_ret10 = (spy["Close"].iloc[-1] / spy["Close"].iloc[-11] - 1) * 100

    rows = {t: raw_factors(histories[t], spy_ret10) for t in universe if t in histories}
    if not rows:
        return []

    weights = FACTOR_WEIGHTS[bucket]
    fdf = pd.DataFrame(rows).T  # rows=tickers, cols=factors
    if len(fdf) >= 3:
        composite = sum(weights[f] * _zscore(fdf[f]) for f in weights)
        scores = 50 + 10 * composite
    else:
        # Degenerate universe (data outage): can't normalize — rank by primary raw
        # factor instead of dividing by a near-zero cross-sectional std.
        primary = "ret10" if bucket == "core" else "ret5"
        scores = 50 + pd.to_numeric(fdf[primary], errors="coerce").fillna(0.0).clip(-25, 25)

    ranked = []
    for t in fdf.index:
        s = round(float(scores[t]), 2)
        ranked.append({
            "ticker": t,
            "bucket": bucket,
            "score": s,
            "last_price": float(fdf.loc[t, "last_price"]),
            "vol20": float(fdf.loc[t, "vol20"]),
            "atr_pct": round(float(fdf.loc[t, "atr_pct"]), 3),
            "rsi14": round(float(fdf.loc[t, "rsi14"]), 2),
            "above_ma50": round(float(fdf.loc[t, "above_ma50"]), 2),
            "sharpe": round(float(fdf.loc[t, "sharpe"]), 3),
            "vol_confirm": round(float(fdf.loc[t, "vol_confirm"]), 2),
            "conviction": min(5, max(1, int(round((s - 50) / 8 + 3)))),
            "thesis": _make_thesis(bucket, t, s),
        })
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked[:top_n]


def _make_thesis(bucket: str, ticker: str, score: float) -> str:
    if bucket == "core":
        return f"{ticker} screening positive on momentum + relative strength (score {score:.1f})"
    if bucket == "swing":
        return f"{ticker} showing volatility expansion + short-term momentum (score {score:.1f})"
    return f"{ticker} high-vol asymmetric setup (score {score:.1f})"
