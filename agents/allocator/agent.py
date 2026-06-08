"""Allocator: free-tier. Deterministic bucket-aware sizing. No LLM cost. Executes via Alpaca."""
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents import broker
from agents._state import (
    BUCKETS,
    PENDING_TRADES_FILE,
    WATCHLIST_FILE,
    append_order_log,
    is_autonomous,
    load_state,
    lottery_remaining_budget,
    save_state,
)

load_dotenv()


# Max position counts per bucket.
BUCKET_MAX_POSITIONS = {"core": 5, "swing": 2, "lottery": 2}


def _bucket_target_positions(bucket: str, remaining: float) -> int:
    """How many positions to open in this bucket given remaining budget."""
    if bucket == "core":
        return min(BUCKET_MAX_POSITIONS["core"], max(1, int(remaining // 40)))
    if bucket == "swing":
        return min(BUCKET_MAX_POSITIONS["swing"], max(1, int(remaining // 22)))
    return min(BUCKET_MAX_POSITIONS["lottery"], max(1, int(remaining // 7)))


def _attach_bucket(positions: list[dict], state: dict) -> list[dict]:
    bucket_map: dict[str, str] = {}
    for entry in state.get("order_log", []):
        if entry.get("side") == "buy" and entry.get("ticker") and entry.get("bucket"):
            bucket_map.setdefault(entry["ticker"], entry["bucket"])
    return [{**p, "bucket": bucket_map.get(p["ticker"], "unassigned")} for p in positions]


def bucket_remaining_budget(state: dict, current_positions: list[dict], bucket: str) -> float:
    target = state["allocation_targets"][bucket]["target_dollars"]
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
    if not watchlist:
        return {"placed_orders": [], "suggestions": [], "note": "Watchlist empty."}

    account = broker.account_info()
    positions = _attach_bucket(broker.positions(), state)
    held_tickers = {p["ticker"] for p in positions}
    suggestions: list[dict] = []

    for bucket in BUCKETS:
        remaining = bucket_remaining_budget(state, positions, bucket)
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
        per_position_budget = remaining / n_pos
        for cand in bucket_candidates[:n_pos]:
            price = cand.get("last_price") or broker.latest_price(cand["ticker"])
            if not price or price <= 0:
                continue
            shares = round(per_position_budget / price, 4)
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
        if spent_per_bucket[s["bucket"]] + s["estimated_cost"] > bucket_remaining_budget(state, positions, s["bucket"]):
            continue
        spent_per_bucket[s["bucket"]] += s["estimated_cost"]
        spent_total += s["estimated_cost"]
        safe.append(s)

    # Note: we do NOT gate on broker.is_market_open(). Alpaca accepts DAY limit
    # orders submitted outside market hours and queues them for the next open.
    # This lets the 9:00 ET premarket cycle place orders that fill at 9:30 ET.
    placed = []
    if is_autonomous(state):
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
    else:
        PENDING_TRADES_FILE.write_text(json.dumps(safe, indent=2))

    return {
        "placed_orders": placed,
        "suggestions": safe,
        "autonomous": is_autonomous(state),
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
