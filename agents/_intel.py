"""Extra intel sources — Alpha Vantage news sentiment + yfinance analyst consensus.
Free tier (AV allows 25 requests/day; research enriches max 15 tickers once daily).
Every source degrades to neutral on failure so screening never blocks."""
import os
import time

import requests
import yfinance as yf

AV_URL = "https://www.alphavantage.co/query"


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
    try:
        info = yf.Ticker(ticker).info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        target = info.get("targetMeanPrice")
        rec = info.get("recommendationMean") or 3.0
        upside = ((target / price) - 1) * 100 if price and target else 0.0
        return float(upside), float(rec)
    except Exception:
        return 0.0, 3.0


def enrich(candidates: list[dict]) -> list[dict]:
    """Blend news sentiment + analyst consensus into each candidate's score, re-rank.
    Composite: momentum + sentiment*15 + capped_upside*0.3 + (3 - rec)*3."""
    for c in candidates:
        t = c["ticker"]
        sent, n_news = news_sentiment(t)
        upside, rec = analyst_view(t)
        c["momentum_score"] = c["score"]
        c["news_sentiment"] = round(sent, 3)
        c["news_count"] = n_news
        c["analyst_upside_pct"] = round(upside, 1)
        c["analyst_rec"] = round(rec, 2)
        c["score"] = round(
            c["score"] + sent * 15 + min(upside, 50) * 0.3 + (3 - rec) * 3, 2
        )
        c["conviction"] = min(5, max(1, int(round(c["score"] / 5 + 3))))
        c["thesis"] = (
            f"{t}: momentum {c['momentum_score']:.0f}, "
            f"news sentiment {sent:+.2f} ({n_news} articles), "
            f"analyst upside {upside:+.0f}% (rec {rec:.1f})"
        )
        time.sleep(1)  # stay well inside free-tier rate limits
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
