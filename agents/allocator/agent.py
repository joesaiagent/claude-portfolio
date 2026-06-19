"""Allocator: free-tier. Deterministic bucket-aware sizing. No LLM cost. Executes via Alpaca."""
import json
import statistics
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents import broker
from agents._regime import current_regime
from agents._state import (
    BUCKETS,
    PENDING_TRADES_FILE,
    WATCHLIST_FILE,
    append_order_log,
    buy_dates,
    is_autonomous,
    load_state,
    lottery_remaining_budget,
    save_state,
    should_execute,
    simulating,
)

load_dotenv()


# Max position counts per bucket. Core concentrated (3) to let conviction bets
# matter — aggressive growth tilt per user direction.
BUCKET_MAX_POSITIONS = {"core": 3, "swing": 2, "lottery": 2}

# Inverse-volatility sizing caps (as a fraction of a bucket's remaining budget),
# so a steady low-vol name gets more capital but no single name dominates a tiny
# account. Floors keep every position clear of Alpaca's $1 min notional.
SIZING_MIN_FRAC = {"core": 0.25, "swing": 0.30, "lottery": 0.45}
SIZING_MAX_FRAC = {"core": 0.55, "swing": 0.55, "lottery": 0.55}
VOL_FLOOR = 0.005


def _inverse_vol_budgets(cands: list[dict], remaining: float, bucket: str) -> list[float]:
    """Dollar budget per candidate via inverse-vol weighting, clamped to per-bucket
    min/max fractions and water-fill renormalized to sum to `remaining`."""
    n = len(cands)
    if n == 0:
        return []
    if n == 1:
        return [remaining]
    inv = [1.0 / max(c.get("vol20") or 0.02, VOL_FLOOR) for c in cands]
    tot = sum(inv)
    budgets = [remaining * x / tot for x in inv]
    # Effective caps must straddle the equal-weight share (1/n) so full deployment
    # is always feasible — otherwise with few names the static caps would leave
    # part of the bucket undeployed (e.g. 2 core names capped at 0.35 = only 70%).
    lo = min(SIZING_MIN_FRAC.get(bucket, 0.0), 1.0 / n) * remaining
    hi = max(SIZING_MAX_FRAC.get(bucket, 1.0), 1.0 / n) * remaining
    # Iterative water-filling: clamp to [lo, hi], then push the residual onto the
    # names that can still move in its direction. Converges to sum==remaining
    # whenever feasible; if the caps make full deployment impossible it leaves the
    # remainder as cash (conservative, never over-deploys).
    for _ in range(20):
        budgets = [min(max(b, lo), hi) for b in budgets]
        residual = remaining - sum(budgets)
        if abs(residual) < 1e-6:
            break
        if residual > 0:
            free = [i for i in range(n) if budgets[i] < hi - 1e-9]
        else:
            free = [i for i in range(n) if budgets[i] > lo + 1e-9]
        if not free:
            break
        add = residual / len(free)
        for i in free:
            budgets[i] += add
    return budgets


def _bucket_target_positions(bucket: str, remaining: float) -> int:
    """How many positions to open in this bucket given remaining budget."""
    if bucket == "core":
        return min(BUCKET_MAX_POSITIONS["core"], max(1, int(remaining // 70)))
    if bucket == "swing":
        return min(BUCKET_MAX_POSITIONS["swing"], max(1, int(remaining // 22)))
    return min(BUCKET_MAX_POSITIONS["lottery"], max(1, int(remaining // 7)))


def _attach_bucket(positions: list[dict], state: dict) -> list[dict]:
    bucket_map: dict[str, str] = {}
    for entry in state.get("order_log", []):
        if entry.get("side") == "buy" and entry.get("ticker") and entry.get("bucket"):
            bucket_map.setdefault(entry["ticker"], entry["bucket"])
    return [{**p, "bucket": bucket_map.get(p["ticker"], "unassigned")} for p in positions]


# --- Rotation: sell the weakest holding to fund a materially stronger fresh name ---
# Wide margins + min-hold + 1/bucket/day churn cap keep this conservative. Lottery
# rotation is disabled (999) — churning a buy-and-pray bucket just bleeds spread.
ROTATE_MARGIN = {"core": 12.0, "swing": 15.0, "lottery": 999.0}  # score points of edge required
ROTATE_MIN_HOLD = {"core": 5.0, "swing": 3.0, "lottery": 9999.0}  # calendar days
# Cash account = T+1 settlement, so the freed cash isn't spendable this cycle.
# Sell now, queue the replacement buy for the next premarket cycle.
ROTATION_SAME_CYCLE_BUY = False


def _rotation_candidates(state: dict, watchlist: list[dict], positions: list[dict]) -> tuple[list[dict], dict]:
    """Decide rotations for full buckets. Returns (actions, updated rotations_today).
    Each action = {bucket, sell (a held position), buy (a fresh candidate), margin}."""
    today = datetime.now(timezone.utc).date().isoformat()
    rot = state.get("rotations_today", {})
    if rot.get("date") != today:
        rot = {"date": today}  # reset the daily churn counter
    dates = buy_dates(state)
    now = datetime.now(timezone.utc)
    score_by_ticker = {c["ticker"]: c.get("score", 50.0) for c in watchlist}
    actions = []

    for bucket in BUCKETS:
        if ROTATE_MARGIN.get(bucket, 999.0) >= 999.0:
            continue
        held = [p for p in positions if p.get("bucket") == bucket]
        if len(held) < BUCKET_MAX_POSITIONS[bucket]:
            continue  # not full -> normal allocation has room, no need to rotate
        if rot.get(bucket, 0) >= 1:
            continue  # churn cap: at most one rotation per bucket per day
        held_set = {p["ticker"] for p in held}
        cands = [c for c in watchlist if c.get("bucket") == bucket and c["ticker"] not in held_set]
        if not cands:
            continue
        best = max(cands, key=lambda c: c.get("score", 0.0))
        # Score held names from the fresh watchlist; unknowns fall back to the
        # bucket's candidate median (conservative — won't force a rotation).
        median = statistics.median([c.get("score", 50.0) for c in cands])
        def hscore(t):
            return score_by_ticker.get(t, median)
        weakest = min(held, key=lambda p: hscore(p["ticker"]))
        margin = best.get("score", 0.0) - hscore(weakest["ticker"])
        if margin < ROTATE_MARGIN[bucket]:
            continue
        held_days = (now - dates[weakest["ticker"]]).total_seconds() / 86400 if weakest["ticker"] in dates else 0.0
        if held_days < ROTATE_MIN_HOLD.get(bucket, 9999.0):
            continue
        actions.append({"bucket": bucket, "sell": weakest, "buy": best, "margin": round(margin, 2)})
        rot[bucket] = rot.get(bucket, 0) + 1

    return actions, rot


def bucket_remaining_budget(state: dict, current_positions: list[dict], bucket: str,
                            exposure: float = 1.0) -> float:
    # `exposure` (<=1.0) is the regime multiplier: it shrinks the effective target
    # in downtrends so the book holds cash instead of deploying fully.
    target = state["allocation_targets"][bucket]["target_dollars"] * exposure
    deployed = sum(
        p["shares"] * p["entry_price"]
        for p in current_positions
        if p.get("bucket") == bucket
    )
    if bucket == "lottery":
        return min(target - deployed, lottery_remaining_budget(state))
    return max(0.0, target - deployed)


def run() -> dict:
    state = load_state()
    watchlist = json.loads(WATCHLIST_FILE.read_text()) if WATCHLIST_FILE.exists() else []

    # Consume any rotation buys queued by a prior cycle's sell (T+1 cash has now
    # settled). Prepend so they take priority; clear on disk so each is attempted
    # exactly once (if its buy fails this cycle, rotation will re-evaluate later).
    pending = state.get("pending_rotation", [])
    if pending:
        watchlist = pending + watchlist
        if should_execute(state):
            state["pending_rotation"] = []
            save_state(state)

    if not watchlist:
        return {"placed_orders": [], "suggestions": [], "note": "Watchlist empty."}

    account = broker.account_info()
    positions = _attach_bucket(broker.positions(), state)
    held_tickers = {p["ticker"] for p in positions}
    suggestions: list[dict] = []

    # Regime filter: scale how much we deploy. risk_off -> hold more cash.
    exposure, regime_label, regime_info = current_regime()
    if simulating() or exposure < 1.0:
        print(f"[allocator] regime={regime_label} exposure={exposure:.0%} {regime_info}")

    for bucket in BUCKETS:
        remaining = bucket_remaining_budget(state, positions, bucket, exposure)
        if remaining < 1.0:
            continue

        # Filter watchlist to this bucket, exclude already-held, sort by conviction/score.
        bucket_candidates = [
            c for c in watchlist
            if c.get("bucket") == bucket and c.get("ticker") not in held_tickers
        ]
        bucket_candidates.sort(
            key=lambda c: (c.get("conviction", 0), c.get("score", 0)), reverse=True
        )

        n_pos = _bucket_target_positions(bucket, remaining)
        chosen = bucket_candidates[:n_pos]
        # Inverse-vol sizing needs vol20 on every candidate; a stale watchlist
        # without it degrades safely to equal weight.
        if chosen and all(c.get("vol20") for c in chosen):
            budgets = _inverse_vol_budgets(chosen, remaining, bucket)
        else:
            budgets = [remaining / len(chosen)] * len(chosen) if chosen else []
        for cand, budget in zip(chosen, budgets):
            price = cand.get("last_price") or broker.latest_price(cand["ticker"])
            if not price or price <= 0:
                continue
            if budget < 1.0:
                continue
            shares = round(budget / price, 4)
            if shares <= 0:
                continue
            est_cost = round(shares * price, 2)
            if est_cost < 1.0:
                continue
            limit_price = round(price * 1.02, 2)
            suggestions.append({
                "ticker": cand["ticker"],
                "bucket": bucket,
                "shares": shares,
                "max_price": limit_price,
                "estimated_cost": est_cost,
                "rationale": cand.get("thesis", ""),
            })
            held_tickers.add(cand["ticker"])

    # Hard cap: don't exceed cash or per-bucket budgets.
    safe = []
    spent_per_bucket = {b: 0.0 for b in BUCKETS}
    spent_total = 0.0
    cash = account["cash"]
    for s in suggestions:
        if spent_total + s["estimated_cost"] > cash:
            continue
        if spent_per_bucket[s["bucket"]] + s["estimated_cost"] > bucket_remaining_budget(state, positions, s["bucket"], exposure):
            continue
        spent_per_bucket[s["bucket"]] += s["estimated_cost"]
        spent_total += s["estimated_cost"]
        safe.append(s)

    # Note: we do NOT gate on broker.is_market_open(). Alpaca accepts DAY limit
    # orders submitted outside market hours and queues them for the next open.
    # This lets the 9:00 ET premarket cycle place orders that fill at 9:30 ET.
    placed = []
    if should_execute(state):
        for s in safe:
            try:
                order = broker.submit_buy(s["ticker"], s["shares"], s.get("max_price"))
                entry = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "ticker": s["ticker"],
                    "bucket": s["bucket"],
                    "side": "buy",
                    "shares": s["shares"],
                    "limit_price": s.get("max_price"),
                    "estimated_cost": s.get("estimated_cost"),
                    "order_id": order["order_id"],
                    "status": order["status"],
                    "rationale": s.get("rationale", ""),
                }
                if s["bucket"] == "lottery":
                    state["lottery_deployed_total"] = round(
                        state.get("lottery_deployed_total", 0) + s["estimated_cost"], 2
                    )
                append_order_log(state, entry)
                state = load_state()
                placed.append(entry)
            except Exception as e:
                placed.append({"ticker": s["ticker"], "error": str(e)})
        PENDING_TRADES_FILE.write_text("[]")
    elif simulating():
        print(f"=== SIMULATE: {len(safe)} buy order(s) NOT submitted ===")
        for s in safe:
            print(
                f"  [SIM BUY] [{s['bucket']:7s}] {s['ticker']:5s} x{s['shares']} "
                f"@ <=${s['max_price']:.2f} (~${s['estimated_cost']:.2f}) — {s['rationale'][:60]}"
            )
    else:
        PENDING_TRADES_FILE.write_text(json.dumps(safe, indent=2))

    # --- Rotation: sell the weakest holding in a full bucket to fund a much
    # stronger fresh name. Runs after exits (earlier in the cycle) have already
    # pruned, so it only ever considers survivors. Sells now; queues the buy.
    rotations = []
    actions, rot = _rotation_candidates(state, watchlist, positions)
    for a in actions:
        weak, buy, bucket = a["sell"], a["buy"], a["bucket"]
        info = {"bucket": bucket, "sell": weak["ticker"], "buy": buy["ticker"], "margin": a["margin"]}
        if should_execute(state):
            try:
                order = broker.submit_sell(weak["ticker"], weak["shares"])
                state = load_state()
                realized = weak.get("pl_dollars", 0.0)
                state.setdefault("realized_pnl", {}).setdefault(bucket, 0.0)
                state["realized_pnl"][bucket] = round(state["realized_pnl"][bucket] + realized, 2)
                state["rotations_today"] = rot
                if not ROTATION_SAME_CYCLE_BUY:
                    queued = {k: buy.get(k) for k in
                              ("ticker", "bucket", "score", "conviction", "last_price", "vol20", "thesis")}
                    state.setdefault("pending_rotation", []).append(queued)
                append_order_log(state, {
                    "ts": datetime.now(timezone.utc).isoformat(), "ticker": weak["ticker"],
                    "bucket": bucket, "side": "sell", "shares": weak["shares"],
                    "reason": f"rotation -> {buy['ticker']} (margin {a['margin']})",
                    "realized_pl_dollars": round(realized, 2),
                })
                info["status"] = "sold; buy queued next cycle" if not ROTATION_SAME_CYCLE_BUY else "sold"
            except Exception as e:
                info["error"] = str(e)[:200]
        elif simulating():
            print(f"  [SIM ROTATE] [{bucket:7s}] SELL {weak['ticker']} -> queue BUY {buy['ticker']} "
                  f"(margin {a['margin']})")
        rotations.append(info)

    return {
        "placed_orders": placed,
        "suggestions": safe,
        "rotations": rotations,
        "regime": regime_label,
        "exposure": exposure,
        "autonomous": is_autonomous(state),
        "simulated": simulating(),
        "market_open": broker.is_market_open(),
    }


if __name__ == "__main__":
    r = run()
    if r["placed_orders"]:
        print(f"=== AUTONOMOUS: placed {len(r['placed_orders'])} order(s) ===")
        for o in r["placed_orders"]:
            if "error" in o:
                print(f"  ✗ {o['ticker']}: {o['error']}")
            else:
                print(f"  ✓ [{o['bucket']}] {o['ticker']} x{o['shares']} @ ≤${o.get('limit_price'):.2f}  status={o['status']}")
    else:
        print(f"=== SUGGESTIONS (not executed — autonomous={r['autonomous']}, market_open={r['market_open']}) ===")
        for s in r["suggestions"]:
            print(f"  [{s['bucket']}] {s['ticker']} x{s['shares']} @ ≤${s.get('max_price'):.2f}  (~${s.get('estimated_cost'):.2f})")
