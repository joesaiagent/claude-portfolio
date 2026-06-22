"""Extra intel sources — Alpha Vantage news sentiment, yfinance analyst consensus,
Finnhub insider data, QuiverQuant congressional trades, and options put/call ratio.
Free tier throughout. Every source degrades to neutral on failure so screening
never blocks. Sources needing a key return 0.0 when the key is absent."""
import os
import time
from datetime import date, timedelta

import requests
import yfinance as yf

from agents._screener import EXCLUDED_SECTORS

AV_URL = "https://www.alphavantage.co/query"
FINNHUB_URL = "https://finnhub.io/api/v1"
QUIVER_URL = "https://api.quiverquant.com/beta"


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


def finnhub_insider_transactions(ticker: str) -> float:
    """Dollar-weighted net insider buying over last 90 days via Finnhub transactions.
    More granular than MSPR: sums actual buy/sell $ amounts, returns [-1, 1].
    Positive = insiders net buying."""
    key = os.getenv("FINNHUB_KEY")
    if not key:
        return 0.0
    try:
        to = date.today()
        frm = to - timedelta(days=90)
        r = requests.get(f"{FINNHUB_URL}/stock/insider-transactions",
                         params={"symbol": ticker, "from": frm.isoformat(),
                                 "to": to.isoformat(), "token": key}, timeout=15).json()
        data = r.get("data", [])
        if not data:
            return 0.0
        buy_val = sum(abs(d.get("change", 0)) * (d.get("price") or 0)
                      for d in data if d.get("change", 0) > 0)
        sell_val = sum(abs(d.get("change", 0)) * (d.get("price") or 0)
                       for d in data if d.get("change", 0) < 0)
        total = buy_val + sell_val
        if total < 1:
            return 0.0
        return max(-1.0, min(1.0, (buy_val - sell_val) / total))
    except Exception:
        return 0.0


def quiverquant_congressional(ticker: str) -> float:
    """Net congressional trading signal (QuiverQuant free tier).
    Counts purchase vs sale disclosures in the last 90 days.
    Returns [-1, 1]: positive = net congressional buying."""
    key = os.getenv("QUIVERQUANT_KEY")
    if not key:
        return 0.0
    try:
        r = requests.get(f"{QUIVER_URL}/historical/congresstrading/{ticker}",
                         headers={"Authorization": f"Token {key}"}, timeout=15).json()
        if not isinstance(r, list) or not r:
            return 0.0
        cutoff = (date.today() - timedelta(days=90)).isoformat()
        recent = [t for t in r if (t.get("Date") or "") >= cutoff]
        if not recent:
            return 0.0
        buys = sum(1 for t in recent
                   if "purchase" in (t.get("Transaction") or "").lower())
        sells = sum(1 for t in recent
                    if "sale" in (t.get("Transaction") or "").lower())
        total = buys + sells
        if total == 0:
            return 0.0
        return (buys - sells) / total
    except Exception:
        return 0.0


def options_pcr(ticker: str) -> float:
    """Put/call ratio signal from yfinance nearest-expiry options chain (free).
    Low PCR = calls dominating = bullish. Returns [-1, 1].
    Skipped for thin chains (< 500 total contracts) to avoid noise."""
    try:
        tk = yf.Ticker(ticker)
        exps = tk.options
        if not exps:
            return 0.0
        chain = tk.option_chain(exps[0])
        call_vol = float(chain.calls["volume"].fillna(0).sum())
        put_vol = float(chain.puts["volume"].fillna(0).sum())
        total = call_vol + put_vol
        if total < 500:
            return 0.0
        pcr = put_vol / call_vol if call_vol > 0 else 3.0
        # Map: pcr 0.5 → +0.67 (bullish), pcr 1.0 → neutral, pcr 1.5 → -0.33 (bearish)
        signal = (1.0 - pcr) / 1.5
        return max(-1.0, min(1.0, signal))
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


# Alt-data overlay is a BOUNDED tilt on top of the price/momentum score.
# Each source nudges but none dominates — the cap keeps momentum primary.
ALT_TILT_CAP = 15.0


def enrich(candidates: list[dict]) -> list[dict]:
    """Blend all alt-data signals into each candidate's score and re-rank.

    Tilt formula (capped at ±15):
      news sentiment      × 6   (Alpha Vantage, ±1 scale)
      Finnhub analyst rec × 4   (consensus strength)
      insider MSPR        × 3   (aggregate monthly purchase ratio)
      insider transactions× 4   (dollar-weighted buys vs sells, 90d)
      congressional       × 4   (net congress buy/sell disclosures, 90d)
      options PCR         × 4   (put/call ratio skew)
      analyst upside      × 0.15 (% to mean price target, capped at 40%)
      analyst rec score   × 2   (1=strong buy → 5=strong sell, inverted)
    """
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
        insider_mspr = finnhub_insider_sentiment(t)
        insider_txn = finnhub_insider_transactions(t)
        congress = quiverquant_congressional(t)
        pcr = options_pcr(t)
        c["finnhub_rec"] = round(fin, 3)
        c["insider_mspr"] = round(insider_mspr, 3)
        c["insider_transactions"] = round(insider_txn, 3)
        c["congressional"] = round(congress, 3)
        c["options_pcr_signal"] = round(pcr, 3)
        tilt = max(-ALT_TILT_CAP, min(ALT_TILT_CAP,
                   sent * 6
                   + fin * 4
                   + insider_mspr * 3
                   + insider_txn * 4
                   + congress * 4
                   + pcr * 4
                   + min(upside, 40) * 0.15
                   + (3 - rec) * 2))
        c["score"] = round(c["score"] + tilt, 2)
        c["conviction"] = min(5, max(1, int(round((c["score"] - 50) / 8 + 3))))
        c["thesis"] = (
            f"{t}: momentum {c['momentum_score']:.0f}, "
            f"news {sent:+.2f} ({n_news}), "
            f"insider txn {insider_txn:+.2f}, "
            f"congress {congress:+.2f}, "
            f"options PCR {pcr:+.2f}, "
            f"analyst {upside:+.0f}% (rec {rec:.1f})"
        )
        time.sleep(1)  # stay inside free-tier rate limits
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
