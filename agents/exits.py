"""Exits: free-tier. Deterministic stop-loss / trailing-stop / take-profit / trend
-break / max-hold rules. Runs before the allocator so freed cash is redeployable.
No LLM cost.

Design invariant: the original hard stops (-15 / -8 / -60) are preserved as
floors. Trailing stops, trend-break and the take-profit only ADD sell triggers —
they never loosen the worst-case behavior. Trailing stops are measured against
each position's HIGH-WATER-MARK (persisted by the tracker), not just entry, so
winners are protected after a runup.
"""
from datetime import datetime, timezone

from dotenv import load_dotenv

import yfinance as yf

from agents import broker
from agents._screener import EXCLUDED_SECTORS, fetch_history
from agents._state import (
    append_order_log,
    bucket_map_from_log,
    buy_dates,
    load_state,
    save_state,
    should_execute,
    simulating,
)

load_dotenv()

RULES = {
    # hard_stop = floor vs entry; trail_pct = give-back from peak once armed at trail_arm_pct.
    # Core is tuned to LET WINNERS RUN (wide trailing, no trend-break) — backtests
    # showed that captures the bulk of momentum's upside; the -15 hard stop + a
    # wide 25% give-back from peak still cap a blowup. (Goal: fastest survivable growth.)
    "core":    {"hard_stop_pct": -15.0, "trail_pct": 25.0, "trail_arm_pct": 15.0, "trend_break": False},
    "swing":   {"hard_stop_pct": -8.0,  "trail_pct": 7.0,  "trail_arm_pct": 6.0,
                "target_pct": 20.0, "max_hold_days": 10},
    "lottery": {"hard_stop_pct": -60.0, "trail_pct": 25.0, "trail_arm_pct": 60.0, "trim_pct": 50.0},
}

TREND_BREAK_BAND = -0.02  # core: sell if price is >2% below its 50-day MA


def _excluded_sector_tickers(tickers: list[str]) -> set[str]:
    """Return tickers whose yfinance sector is in EXCLUDED_SECTORS.
    Degrades to empty set on any data failure — never a spurious sell."""
    out: set[str] = set()
    for t in tickers:
        try:
            sector = yf.Ticker(t).info.get("sector", "")
            if sector in EXCLUDED_SECTORS:
                out.add(t)
        except Exception:
            pass
    return out


def _decide(pos: dict, bucket: str, held_days: float, high_pl_pct: float,
            trend_broken: bool, already_trimmed: bool) -> tuple[float, str] | None:
    """Return (fraction_to_sell, reason) or None to hold. Priority order matters."""
    rules = RULES.get(bucket)
    if not rules:
        return None
    pl = pos["pl_pct"]
    peak = max(high_pl_pct, pl)  # fold live gain into the peak so an intra-cycle spike trails

    # 1. Hard stop vs entry — the preserved safety floor.
    if pl <= rules["hard_stop_pct"]:
        return 1.0, f"hard stop ({pl:+.1f}%)"
    # 2. Trailing stop vs peak, only once armed (avoids stopping out barely-green noise).
    arm, trail = rules.get("trail_arm_pct"), rules.get("trail_pct")
    if arm is not None and trail is not None and peak >= arm and pl <= peak - trail:
        return 1.0, f"trailing stop (peak {peak:+.1f}%, now {pl:+.1f}%)"
    # 3. Take-profit (swing only).
    if "target_pct" in rules and pl >= rules["target_pct"]:
        return 1.0, f"profit target ({pl:+.1f}%)"
    # 4. Lottery trim — half off the table, ONCE (guarded so it can't re-fire each cycle).
    if "trim_pct" in rules and pl >= rules["trim_pct"] and not already_trimmed:
        return 0.5, f"trim half ({pl:+.1f}%)"
    # 5. Core trend-break — momentum gone (price below 50DMA).
    if rules.get("trend_break") and trend_broken:
        return 1.0, f"trend break (<50DMA, {pl:+.1f}%)"
    # 6. Swing max-hold (it's a 2-10 day book; *1.4 ~ trading->calendar days).
    if "max_hold_days" in rules and held_days > rules["max_hold_days"] * 1.4:
        return 1.0, f"max hold exceeded ({held_days:.0f}d, {pl:+.1f}%)"
    return None


def _core_trend_broken(positions: list[dict], buckets: dict[str, str]) -> dict[str, bool]:
    """For held core names, True if last close is >2% below the 50-day MA.
    A data outage degrades to False (never a spurious sell)."""
    core_tickers = [p["ticker"] for p in positions if buckets.get(p["ticker"]) == "core"]
    if not core_tickers:
        return {}
    histories = fetch_history(core_tickers, days=90)
    broken = {}
    for t in core_tickers:
        df = histories.get(t)
        if df is None or len(df) < 50:
            broken[t] = False
            continue
        close = df["Close"].dropna()
        ma50 = close.rolling(50).mean().iloc[-1]
        broken[t] = bool(ma50 and (close.iloc[-1] / ma50 - 1) < TREND_BREAK_BAND)
    return broken


def run() -> dict:
    state = load_state()
    positions = broker.positions()
    if not positions:
        return {"exits": [], "note": "no positions"}

    buckets = bucket_map_from_log(state)
    dates = buy_dates(state)
    highs = state.get("position_highs", {})
    trimmed = set(state.get("lottery_trimmed", []))
    trend_broken = _core_trend_broken(positions, buckets)
    now = datetime.now(timezone.utc)
    exits = []

    excluded = _excluded_sector_tickers([p["ticker"] for p in positions])

    for pos in positions:
        t = pos["ticker"]
        if t in excluded:
            bucket = buckets.get(t, "core")
            shares = pos["shares"]
            reason = "sector excluded (healthcare)"
            record = {"ticker": t, "bucket": bucket, "shares": shares,
                      "fraction": 1.0, "reason": reason, "pl_pct": pos["pl_pct"]}
            if should_execute(state):
                try:
                    order = broker.submit_sell(t, shares)
                    record["order_id"] = order["order_id"]
                    record["status"] = order["status"]
                    realized = pos["pl_dollars"]
                    state.setdefault("realized_pnl", {}).setdefault(bucket, 0.0)
                    state["realized_pnl"][bucket] = round(state["realized_pnl"][bucket] + realized, 2)
                    append_order_log(state, {
                        "ts": now.isoformat(), "ticker": t, "bucket": bucket, "side": "sell",
                        "shares": shares, "reason": reason,
                        "realized_pl_dollars": round(realized, 2),
                    })
                except Exception as e:
                    record["error"] = str(e)[:200]
            elif simulating():
                print(f"  [SIM SELL] [{bucket:7s}] {t:5s} 100% x{shares} — {reason}")
            exits.append(record)
            print(f"[exits] {t} ({bucket}): SELL 100% — {reason}")
            continue

    for pos in positions:
        t = pos["ticker"]
        bucket = buckets.get(t, "core")  # unknown -> most conservative stop
        held_days = (now - dates[t]).total_seconds() / 86400 if t in dates else 0.0
        high_pl_pct = highs.get(t, {}).get("high_pl_pct", 0.0)
        decision = _decide(pos, bucket, held_days, high_pl_pct,
                           trend_broken.get(t, False), t in trimmed)
        if not decision:
            continue
        fraction, reason = decision

        # Dust guard: fully flatten with the exact share count (no rounded remainder);
        # for partials, round and skip anything below Alpaca's $1 min notional.
        if fraction >= 1.0:
            shares = pos["shares"]
        else:
            shares = round(pos["shares"] * fraction, 4)
        notional = shares * pos.get("current_price", 0)
        if shares <= 0 or notional < 1.0:
            exits.append({"ticker": t, "bucket": bucket, "reason": reason,
                          "skipped": f"notional ${notional:.2f} < $1"})
            continue

        record = {"ticker": t, "bucket": bucket, "shares": shares,
                  "fraction": fraction, "reason": reason, "pl_pct": pos["pl_pct"]}

        if should_execute(state):
            try:
                order = broker.submit_sell(t, shares)
                record["order_id"] = order["order_id"]
                record["status"] = order["status"]
                realized = pos["pl_dollars"] * fraction
                state.setdefault("realized_pnl", {}).setdefault(bucket, 0.0)
                state["realized_pnl"][bucket] = round(state["realized_pnl"][bucket] + realized, 2)
                if fraction < 1.0 and bucket == "lottery":
                    trimmed.add(t)
                    state["lottery_trimmed"] = sorted(trimmed)
                append_order_log(state, {
                    "ts": now.isoformat(), "ticker": t, "bucket": bucket, "side": "sell",
                    "shares": shares, "reason": reason,
                    "realized_pl_dollars": round(realized, 2),
                })
            except Exception as e:
                record["error"] = str(e)[:200]
        elif simulating():
            print(f"  [SIM SELL] [{bucket:7s}] {t:5s} {fraction:.0%} x{shares} — {reason}")

        exits.append(record)
        print(f"[exits] {t} ({bucket}): SELL {fraction:.0%} — {reason}")

    if should_execute(state):
        save_state(state)
    return {"exits": exits}


if __name__ == "__main__":
    print(run())
