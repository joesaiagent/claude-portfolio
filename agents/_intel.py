"""Extra intel sources — Alpha Vantage news sentiment + yfinance analyst consensus.
Free tier (AV allows 25 requests/day; research enriches max 15 tickers once daily).
Every source degrades to neutral on failure so screening never blocks."""
import os
import time
from datetime import date, timedelta

import requests
import yfinance as yf

from agents._screener import EXCLUDED_SECTORS

AV_URL = "https://www.alphavantage.co/query"
FINNHUB_URL = "https://finnhub.io/api/v1"


def finnhub_rec(ticker: str) -> float:
    """Finnhub analyst recommendation trend, mapped to ~[-1, 1] (bullish positive).
    Free endpoint. Returns 0.0 (neutral) with no key or on any failure."""
    key = os.getenv("FINNHUB_KEY")
    if not key:
        return 0.0
    try:
        r = requests.get(f"{FINNHUB_URL}/stock/recommendation",
                         params={"symbol": ticker, "token": key}, timeout=15).json()
        if not r:
            return 0.0
        latest = r[0]
        sb, b = latest.get("strongBuy", 0), latest.get("buy", 0)
        h, s, ss = latest.get("hold", 0), latest.get("sell", 0), latest.get("strongSell", 0)
        total = sb + b + h + s + ss
        if not total:
            return 0.0
        return (sb + 0.5 * b - 0.5 * s - ss) / total
    except Exception:
        return 0.0


def finnhub_insider_sentiment(ticker: str) -> float:
    """Finnhub insider MSPR (monthly share-purchase ratio), latest month, mapped
    to [-1, 1]. Positive = insiders net buying (bullish 30-90d per Finnhub).
    Free endpoint. Neutral 0.0 with no key or on failure."""
    key = os.getenv("FINNHUB_KEY")
    if not key:
        return 0.0
    try:
        to = date.today()
        frm = to - timedelta(days=180)
        r = requests.get(f"{FINNHUB_URL}/stock/insider-sentiment",
                         params={"symbol": ticker, "from": frm.isoformat(),
                                 "to": to.isoformat(), "token": key}, timeout=15).json()
        data = r.get("data", [])
        if not data:
            return 0.0
        latest = max(data, key=lambda d: (d.get("year", 0), d.get("month", 0)))
        return max(-1.0, min(1.0, latest.get("mspr", 0) / 100.0))
    except Exception:
        return 0.0


def news_sentiment(ticker: str) -> tuple[float, int]:
    """Relevance-weighted mean sentiment of recent news. Returns (score, n_articles).
    Score roughly in [-1, 1]; 0.0 = neutral / no data / no API key."""
    key = os.getenv("ALPHA_VANTAGE_KEY")
    if not key:
        return 0.0, 0
    try:
        r = requests.get(AV_URL, params={
            "function": "NEWS_SENTIMENT", "tickers": ticker,
            "sort": "LATEST", "limit": 50, "apikey": key,
        }, timeout=15).json()
        feed = r.get("feed", [])
        num = den = 0.0
        for art in feed:
            for ts in art.get("ticker_sentiment", []):
                if ts.get("ticker") == ticker:
                    rel = float(ts.get("relevance_score", 0))
                    num += rel * float(ts.get("ticker_sentiment_score", 0))
                    den += rel
        return (num / den if den else 0.0), len(feed)
    except Exception:
        return 0.0, 0


def analyst_view(ticker: str) -> tuple[float, float]:
    """(upside % to mean analyst target, recommendation 1=strong buy..5=sell).
    ETFs and failures return neutral (0% upside, rec 3.0)."""
    upside, rec, _ = fundamentals(ticker)
    return upside, rec


def fundamentals(ticker: str) -> tuple[float, float, str]:
    """One yfinance .info call -> (upside % to mean target, recommendation, sector).
    ETFs/failures return neutral (0% upside, rec 3.0, "")."""
    try:
        info = yf.Ticker(ticker).info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        target = info.get("targetMeanPrice")
        rec = info.get("recommendationMean") or 3.0
        upside = ((target / price) - 1) * 100 if price and target else 0.0
        return float(upside), float(rec), info.get("sector") or ""
    except Exception:
        return 0.0, 3.0, ""


# News/analyst overlay is a BOUNDED tilt on top of the price/momentum score, so
# fundamentals nudge but never dominate a ~50-centered momentum score.
NEWS_TILT_CAP = 12.0


def enrich(candidates: list[dict]) -> list[dict]:
    """Blend news sentiment + analyst consensus into each candidate's score, re-rank.
    Tilt = clip(sentiment*6 + capped_upside*0.15 + (3 - rec)*2, ±12) added to momentum."""
    for c in candidates:
        t = c["ticker"]
        sent, n_news = news_sentiment(t)
        upside, rec, sector = fundamentals(t)
        c["momentum_score"] = c["score"]
        c["news_sentiment"] = round(sent, 3)
        c["news_count"] = n_news
        c["analyst_upside_pct"] = round(upside, 1)
        c["analyst_rec"] = round(rec, 2)
        c["sector"] = sector
        c["excluded"] = sector in EXCLUDED_SECTORS
        fin = finnhub_rec(t)
        insider = finnhub_insider_sentiment(t)
        c["finnhub_rec"] = round(fin, 3)
        c["insider_mspr"] = round(insider, 3)
        tilt = max(-NEWS_TILT_CAP, min(NEWS_TILT_CAP,
                   sent * 6 + fin * 4 + insider * 5 + min(upside, 40) * 0.15 + (3 - rec) * 2))
        c["score"] = round(c["score"] + tilt, 2)
        c["conviction"] = min(5, max(1, int(round((c["score"] - 50) / 8 + 3))))
        c["thesis"] = (
            f"{t}: momentum {c['momentum_score']:.0f}, "
            f"news sentiment {sent:+.2f} ({n_news} articles), "
            f"analyst upside {upside:+.0f}% (rec {rec:.1f})"
        )
        time.sleep(1)  # stay well inside free-tier rate limits
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
