"""Free-tier screening helpers — yfinance + Python rules, no LLM."""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)


# Curated universes — tweakable, no API cost to scan.
CORE_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD", "AVGO", "ORCL",
    "COST", "WMT", "JPM", "V", "MA", "LLY", "UNH", "JNJ", "PG", "HD",
    "QQQ", "SPY", "SMH", "XLK", "XLF", "XLE", "XLV", "IWM", "SCHG", "VOOG",
]

SWING_UNIVERSE = [
    "NVDA", "AMD", "PLTR", "SOFI", "RBLX", "COIN", "HOOD", "DIS", "NFLX",
    "UBER", "ABNB", "SHOP", "CRWD", "SNOW", "DDOG", "ZS", "NET", "MDB", "OKTA",
]

LOTTERY_UNIVERSE = [
    "SOUN", "BBAI", "RGTI", "QBTS", "IONQ", "ACHR", "JOBY", "ASTS", "RKLB",
    "OPEN", "MARA", "RIOT", "CLSK", "WULF", "TLRY",
]


def fetch_history(tickers: list[str], days: int = 90) -> dict[str, pd.DataFrame]:
    """Pull OHLCV history for a list of tickers. Returns {ticker: dataframe}."""
    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        data = yf.download(
            tickers, start=start, end=end, group_by="ticker", auto_adjust=True,
            progress=False, threads=True,
        )
    except Exception:
        return {}

    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
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


def score_momentum(df: pd.DataFrame, spy: pd.DataFrame | None = None) -> float:
    """Composite momentum score:
       above 50-day MA + 10-day return + relative strength vs SPY.
       Higher = stronger trend."""
    close = df["Close"]
    if len(close) < 50:
        return -999.0
    ma50 = close.rolling(50).mean().iloc[-1]
    last = close.iloc[-1]
    ret10 = (close.iloc[-1] / close.iloc[-11] - 1) * 100 if len(close) > 11 else 0
    above_ma = (last / ma50 - 1) * 100

    rs = 0.0
    if spy is not None and len(spy) > 11:
        spy_ret10 = (spy["Close"].iloc[-1] / spy["Close"].iloc[-11] - 1) * 100
        rs = ret10 - spy_ret10

    return float(above_ma) + float(ret10) + float(rs)


def score_swing(df: pd.DataFrame) -> float:
    """Swing score: short-term momentum + recent volatility expansion (catalysts often
    show as vol pickups). No earnings calendar dependency."""
    close = df["Close"]
    if len(close) < 20:
        return -999.0
    ret5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) > 6 else 0
    vol_recent = close.pct_change().tail(5).std() * 100
    vol_long = close.pct_change().tail(30).std() * 100
    vol_expansion = (vol_recent / vol_long - 1) * 100 if vol_long > 0 else 0
    return float(ret5) + float(vol_expansion) * 0.5


def score_lottery(df: pd.DataFrame) -> float:
    """Lottery score: high recent return + high volatility (asymmetric setups)."""
    close = df["Close"]
    if len(close) < 10:
        return -999.0
    ret5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) > 6 else 0
    vol = close.pct_change().tail(20).std() * 100
    return float(ret5) + float(vol) * 2.0


def screen_bucket(bucket: str, top_n: int = 5) -> list[dict]:
    """Return ranked candidates for a bucket. No LLM calls."""
    universe_map = {
        "core": CORE_UNIVERSE,
        "swing": SWING_UNIVERSE,
        "lottery": LOTTERY_UNIVERSE,
    }
    universe = universe_map[bucket]
    histories = fetch_history(universe + (["SPY"] if bucket == "core" else []), days=90)
    spy = histories.get("SPY") if bucket == "core" else None

    ranked = []
    for t in universe:
        if t not in histories:
            continue
        df = histories[t]
        if bucket == "core":
            s = score_momentum(df, spy=spy)
        elif bucket == "swing":
            s = score_swing(df)
        else:
            s = score_lottery(df)
        last_price = float(df["Close"].iloc[-1])
        ranked.append({
            "ticker": t,
            "bucket": bucket,
            "score": round(s, 2),
            "last_price": round(last_price, 2),
            "conviction": min(5, max(1, int(round(s / 5 + 3)))),
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
