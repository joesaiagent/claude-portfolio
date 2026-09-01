"""Signal attribution: free-tier, no LLM. Which entry signals actually predict
forward returns?

Every buy carries a signal snapshot — structured (`signals`, stamped by the
allocator since 2026-09-01) or as a legacy thesis string ("CRWD: momentum 60,
news +0.20 (50), insider txn +0.00, ...") parsed by regex. This module joins
each buy's signals to the ticker's 10- and 20-trading-day forward return from
the buy-date close, then reports the Spearman rank correlation and a top-half
vs bottom-half mean-return split per signal.

Read it like this: a signal whose correlation hugs 0 across horizons (and whose
top/bottom split is flat) is dead weight in the enrich() tilt — cut or reweight
it. Small n makes any single month noisy; judge on the trend as trades
accumulate, not one report. Runs in the postclose analytics step; the report is
persisted to data/attribution_report.json.
"""
import json
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from agents._screener import fetch_history
from agents._state import DATA_DIR, atomic_write_text, load_state

load_dotenv()

ATTRIBUTION_REPORT = DATA_DIR / "attribution_report.json"

HORIZONS = (10, 20)  # trading days
MIN_TRADES = 5       # below this, correlations are pure noise — report but flag

# Legacy thesis-string patterns (buys before the structured `signals` field).
_THESIS_PATTERNS = {
    "momentum_score": re.compile(r"momentum (\d+(?:\.\d+)?)"),
    "news_sentiment": re.compile(r"news ([+-]?\d+\.\d+)"),
    "insider_transactions": re.compile(r"insider txn ([+-]?\d+\.\d+)"),
    "congressional": re.compile(r"congress ([+-]?\d+\.\d+)"),
    "options_pcr_signal": re.compile(r"options PCR ([+-]?\d+\.\d+)"),
    "analyst_upside_pct": re.compile(r"analyst ([+-]?\d+(?:\.\d+)?)% \(rec"),
    "analyst_rec": re.compile(r"\(rec (\d+(?:\.\d+)?)\)"),
}

# Signals worth testing (numeric, present on most entries one way or another).
SIGNALS = ("score", "momentum_score", "news_sentiment", "insider_transactions",
           "congressional", "options_pcr_signal", "analyst_upside_pct",
           "analyst_rec", "finnhub_rec", "insider_mspr", "rsi14", "above_ma50",
           "atr_pct", "vol20")


def parse_signals(entry: dict) -> dict:
    """Signal dict for one buy entry: the structured snapshot when present,
    else whatever the legacy thesis string yields. Empty dict = nothing usable
    (e.g. pyramid adds, manual buys)."""
    sig = entry.get("signals")
    if isinstance(sig, dict) and any(k in sig for k in SIGNALS):
        return {k: float(v) for k, v in sig.items()
                if k in SIGNALS and isinstance(v, (int, float))}
    out = {}
    text = entry.get("rationale", "") or ""
    for key, pat in _THESIS_PATTERNS.items():
        m = pat.search(text)
        if m:
            out[key] = float(m.group(1))
    return out


def _spearman(x: list[float], y: list[float]) -> float | None:
    """Spearman rank correlation without scipy (average ranks for ties).
    None if degenerate — a constant signal (e.g. the congressional stub)
    carries no information."""
    if len(x) < 3:
        return None
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() < 1e-12 or ry.std() < 1e-12:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _forward_returns(trades: list[dict]) -> list[dict]:
    """Attach fwd10/fwd20 (pct from buy-date close) to each trade; drops trades
    whose history can't be resolved. Close-to-close, so fill noise doesn't
    contaminate the signal test."""
    tickers = sorted({t["ticker"] for t in trades})
    if not tickers:
        return []
    earliest = min(datetime.fromisoformat(t["ts"]) for t in trades)
    days = (datetime.now(timezone.utc) - earliest).days + 40
    hist = fetch_history(tickers, days=days)
    out = []
    for tr in trades:
        df = hist.get(tr["ticker"])
        if df is None:
            continue
        close = df["Close"].dropna()
        buy_day = datetime.fromisoformat(tr["ts"]).date()
        idx = close.index.searchsorted(np.datetime64(buy_day))
        if idx >= len(close):
            continue
        base = float(close.iloc[idx])
        if base <= 0:
            continue
        enriched = dict(tr)
        for h in HORIZONS:
            j = idx + h
            enriched[f"fwd{h}"] = (round((float(close.iloc[j]) / base - 1) * 100, 2)
                                   if j < len(close) else None)
        out.append(enriched)
    return out


def run() -> dict:
    state = load_state()
    trades = []
    for e in state.get("order_log", []):
        if e.get("side") != "buy" or not e.get("ticker") or not e.get("ts"):
            continue
        sig = parse_signals(e)
        if sig:
            trades.append({"ticker": e["ticker"], "ts": e["ts"], **{"sig": sig}})

    trades = _forward_returns(trades)
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "n_trades": len(trades),
        "low_sample": len(trades) < MIN_TRADES,
        "horizons": {},
    }
    lines = [f"Signal attribution over {len(trades)} buys:"]
    for h in HORIZONS:
        stats = {}
        matured = [t for t in trades if t.get(f"fwd{h}") is not None]
        for s in SIGNALS:
            pairs = [(t["sig"][s], t[f"fwd{h}"]) for t in matured if s in t["sig"]]
            if len(pairs) < 3:
                continue
            xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
            rho = _spearman(xs, ys)
            if rho is None:
                continue
            order = np.argsort(xs)
            half = len(order) // 2
            bottom = float(np.mean([ys[i] for i in order[:half]])) if half else 0.0
            top = float(np.mean([ys[i] for i in order[-half:]])) if half else 0.0
            stats[s] = {"spearman": round(rho, 3), "n": len(pairs),
                        "top_half_mean_fwd_pct": round(top, 2),
                        "bottom_half_mean_fwd_pct": round(bottom, 2)}
        report["horizons"][str(h)] = stats
        if stats:
            best = max(stats.items(), key=lambda kv: kv[1]["spearman"])
            worst = min(stats.items(), key=lambda kv: kv[1]["spearman"])
            lines.append(f"  fwd{h}d: best {best[0]} ρ={best[1]['spearman']:+.2f}, "
                         f"worst {worst[0]} ρ={worst[1]['spearman']:+.2f} "
                         f"(n≈{best[1]['n']})")
    if report["low_sample"]:
        lines.append(f"  (only {len(trades)} scored trades — treat as noise until n grows)")

    report["summary"] = "\n".join(lines)
    atomic_write_text(ATTRIBUTION_REPORT, json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    r = run()
    print(r["summary"])
    for h, stats in r["horizons"].items():
        print(f"\n--- fwd{h}d ---")
        for s, v in sorted(stats.items(), key=lambda kv: -kv[1]["spearman"]):
            print(f"  {s:22s} ρ={v['spearman']:+.3f}  n={v['n']:2d}  "
                  f"top {v['top_half_mean_fwd_pct']:+.1f}% vs bottom {v['bottom_half_mean_fwd_pct']:+.1f}%")
