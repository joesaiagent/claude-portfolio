"""Exits: free-tier. Deterministic stop-loss / take-profit / max-hold rules.
Runs before the allocator so freed cash is redeployable. No LLM cost.

Rules per bucket:
- core:    stop -15%. Long-term compounders otherwise left to run.
- swing:   stop -8%, target +15%, max hold 10 trading days (it's a 2-10 day book).
- lottery: sell half at +50% (recover principal, let the rest ride), stop -60%.
"""
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents import broker
from agents._state import append_order_log, is_autonomous, load_state, save_state

load_dotenv()

RULES = {
    "core": {"stop_pct": -15.0},
    "swing": {"stop_pct": -8.0, "target_pct": 15.0, "max_hold_days": 10},
    "lottery": {"stop_pct": -60.0, "trim_pct": 50.0},
}


def _buy_dates(state: dict) -> dict[str, datetime]:
    """Earliest buy timestamp per ticker from the order log."""
    out: dict[str, datetime] = {}
    for e in state.get("order_log", []):
        if e.get("side") == "buy" and e.get("ticker") and e.get("ts"):
            ts = datetime.fromisoformat(e["ts"])
            if e["ticker"] not in out or ts < out[e["ticker"]]:
                out[e["ticker"]] = ts
    return out


def _bucket_map(state: dict) -> dict[str, str]:
    return {
        e["ticker"]: e["bucket"]
        for e in state.get("order_log", [])
        if e.get("side") == "buy" and e.get("ticker") and e.get("bucket")
    }


def _decide(pos: dict, bucket: str, held_days: float) -> tuple[float, str] | None:
    """Return (fraction_to_sell, reason) or None to hold."""
    rules = RULES.get(bucket)
    if not rules:
        return None
    pl = pos["pl_pct"]
    if pl <= rules.get("stop_pct", -100.0):
        return 1.0, f"stop loss ({pl:+.1f}%)"
    if "target_pct" in rules and pl >= rules["target_pct"]:
        return 1.0, f"profit target ({pl:+.1f}%)"
    if "trim_pct" in rules and pl >= rules["trim_pct"]:
        return 0.5, f"trim half at ({pl:+.1f}%)"
    if "max_hold_days" in rules and held_days > rules["max_hold_days"] * 1.4:
        # *1.4 converts trading days to rough calendar days
        return 1.0, f"max hold exceeded ({held_days:.0f}d, {pl:+.1f}%)"
    return None


def run() -> dict:
    state = load_state()
    positions = broker.positions()
    if not positions:
        return {"exits": [], "note": "no positions"}

    buckets = _bucket_map(state)
    buy_dates = _buy_dates(state)
    now = datetime.now(timezone.utc)
    exits = []

    for pos in positions:
        t = pos["ticker"]
        bucket = buckets.get(t, "core")  # unknown -> most conservative stop
        held_days = (now - buy_dates[t]).total_seconds() / 86400 if t in buy_dates else 0.0
        decision = _decide(pos, bucket, held_days)
        if not decision:
            continue
        fraction, reason = decision
        shares = round(pos["shares"] * fraction, 4)
        if shares <= 0:
            continue
        record = {
            "ticker": t,
            "bucket": bucket,
            "shares": shares,
            "fraction": fraction,
            "reason": reason,
            "pl_pct": pos["pl_pct"],
        }
        if is_autonomous(state):
            try:
                order = broker.submit_sell(t, shares)
                record["order_id"] = order["order_id"]
                record["status"] = order["status"]
                # Approximate realized P/L from the position's unrealized P/L share.
                realized = pos["pl_dollars"] * fraction
                state.setdefault("realized_pnl", {}).setdefault(bucket, 0.0)
                state["realized_pnl"][bucket] = round(
                    state["realized_pnl"][bucket] + realized, 2
                )
                append_order_log(state, {
                    "ts": now.isoformat(),
                    "ticker": t,
                    "bucket": bucket,
                    "side": "sell",
                    "shares": shares,
                    "reason": reason,
                    "realized_pl_dollars": round(realized, 2),
                })
            except Exception as e:
                record["error"] = str(e)[:200]
        exits.append(record)
        print(f"[exits] {t} ({bucket}): SELL {fraction:.0%} — {reason}")

    save_state(state)
    return {"exits": exits}


if __name__ == "__main__":
    print(run())
