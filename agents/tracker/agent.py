"""Tracker: free-tier. Reads Alpaca state, computes P/L, templated summary. No LLM cost."""
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents import broker
from agents._state import BUCKETS, TRACKER_REPORT, atomic_write_text, load_state, save_state, simulating

load_dotenv()


def update_position_highs(positions: list[dict], state: dict) -> dict:
    """Maintain a per-position high-water-mark in state, used by the trailing-stop
    exit logic. Tracker is the canonical writer because it runs every cycle
    (including post-close, which captures the closing high) while exits do not.
    Prunes tickers no longer held so a re-bought name starts a fresh peak."""
    highs = state.get("position_highs", {})
    held = set()
    for p in positions:
        t = p["ticker"]
        held.add(t)
        cur = float(p["current_price"])
        entry = float(p["entry_price"])
        high = max(highs.get(t, {}).get("high_price", 0.0), cur)
        highs[t] = {
            "high_price": round(high, 4),
            "high_pl_pct": round((high / entry - 1) * 100, 3) if entry else 0.0,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
    for t in list(highs):
        if t not in held:
            del highs[t]
    state["position_highs"] = highs
    return highs


def merge_positions_with_buckets(broker_positions: list[dict], state: dict) -> list[dict]:
    bucket_map: dict[str, str] = {}
    for entry in state.get("order_log", []):
        if entry.get("side") == "buy" and entry.get("ticker") and entry.get("bucket"):
            bucket_map.setdefault(entry["ticker"], entry["bucket"])
    return [{**p, "bucket": bucket_map.get(p["ticker"], "unassigned")} for p in broker_positions]


def compute_bucket_summary(positions: list[dict], state: dict) -> dict:
    summary = {}
    for b in BUCKETS:
        target = state["allocation_targets"][b]["target_dollars"]
        bucket_pos = [p for p in positions if p.get("bucket") == b]
        cost = sum(p["shares"] * p["entry_price"] for p in bucket_pos)
        value = sum(p["market_value"] for p in bucket_pos)
        summary[b] = {
            "target_dollars": target,
            "deployed_cost_basis": round(cost, 2),
            "current_value": round(value, 2),
            "unrealized_pl_dollars": round(value - cost, 2),
            "unrealized_pl_pct": round((value - cost) / cost * 100, 2) if cost else 0,
            "realized_pl_dollars": state.get("realized_pnl", {}).get(b, 0.0),
            "position_count": len(bucket_pos),
        }
    return summary


def template_summary(report: dict) -> str:
    if not report["positions"]:
        return "Portfolio: $300 cash, 0% deployed. Waiting for next premarket cycle."
    lines = []
    pl = report["total_pl_dollars"]
    pct = report["total_pl_pct"]
    arrow = "📈" if pl >= 0 else "📉"
    lines.append(f"{arrow} Portfolio ${report['total_portfolio_value']:.2f} ({pct:+.2f}%, P/L ${pl:+.2f})")
    for name, b in report["buckets"].items():
        if b["position_count"]:
            lines.append(
                f"  {name:7s}: {b['position_count']} pos, ${b['current_value']:.2f} ({b['unrealized_pl_pct']:+.2f}%)"
            )
    sorted_pos = sorted(report["positions"], key=lambda p: p.get("pl_pct", 0), reverse=True)
    if sorted_pos:
        best = sorted_pos[0]
        worst = sorted_pos[-1]
        lines.append(f"  Best: {best['ticker']} {best.get('pl_pct', 0):+.2f}%  |  Worst: {worst['ticker']} {worst.get('pl_pct', 0):+.2f}%")
    return "\n".join(lines)


def run() -> dict:
    state = load_state()
    account = broker.account_info()
    positions = merge_positions_with_buckets(broker.positions(), state)
    update_position_highs(positions, state)  # persist high-water-marks for trailing stops
    buckets = compute_bucket_summary(positions, state)

    starting = state.get("starting_capital", 300.0)
    portfolio_value = account["portfolio_value"]
    pl_dollars = portfolio_value - starting
    pl_pct = (pl_dollars / starting * 100) if starting else 0

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "broker_mode": "paper" if account["paper"] else "LIVE",
        "positions": positions,
        "buckets": buckets,
        "cash": round(account["cash"], 2),
        "buying_power": round(account["buying_power"], 2),
        "total_invested_value": round(portfolio_value - account["cash"], 2),
        "total_portfolio_value": round(portfolio_value, 2),
        "total_pl_dollars": round(pl_dollars, 2),
        "total_pl_pct": round(pl_pct, 2),
    }
    report["summary"] = template_summary(report)
    if not simulating():
        save_state(state)  # persist position_highs
        atomic_write_text(TRACKER_REPORT, json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    r = run()
    print(f"=== PORTFOLIO ({r['broker_mode']}) ===")
    print(r["summary"])
