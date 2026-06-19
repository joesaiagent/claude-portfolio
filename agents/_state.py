"""Shared portfolio state helpers used by all agents."""
import json
import os
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
STATE_FILE = DATA_DIR / "portfolio_state.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
PENDING_TRADES_FILE = DATA_DIR / "pending_trades.json"
POSTS_FILE = DATA_DIR / "posts_queue.json"
TRACKER_REPORT = DATA_DIR / "tracker_report.json"

BUCKETS = ("core", "swing", "lottery")


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def is_autonomous(state: dict | None = None) -> bool:
    state = state or load_state()
    return bool(state.get("autonomous_mode"))


def should_execute(state: dict | None = None) -> bool:
    """Whether agents may place REAL orders. False whenever SIMULATE is set in
    the environment, so a simulated run computes every decision but submits
    nothing and writes no state. This is the master kill-switch used to validate
    strategy changes against real positions before going live."""
    return is_autonomous(state) and not os.getenv("SIMULATE")


def simulating() -> bool:
    return bool(os.getenv("SIMULATE"))


def bucket_map_from_log(state: dict) -> dict[str, str]:
    """Map ticker -> bucket from the earliest buy in the order log.
    Shared by allocator/exits/tracker so bucket attribution is consistent."""
    bucket_map: dict[str, str] = {}
    for entry in state.get("order_log", []):
        if entry.get("side") == "buy" and entry.get("ticker") and entry.get("bucket"):
            bucket_map.setdefault(entry["ticker"], entry["bucket"])
    return bucket_map


def buy_dates(state: dict) -> dict[str, datetime]:
    """Earliest buy timestamp per ticker from the order log (for hold-time rules)."""
    out: dict[str, datetime] = {}
    for e in state.get("order_log", []):
        if e.get("side") == "buy" and e.get("ticker") and e.get("ts"):
            ts = datetime.fromisoformat(e["ts"])
            if e["ticker"] not in out or ts < out[e["ticker"]]:
                out[e["ticker"]] = ts
    return out


def lottery_remaining_budget(state: dict) -> float:
    cap = state["allocation_targets"]["lottery"]["max_loss_dollars"]
    deployed = state.get("lottery_deployed_total", 0.0)
    return max(0.0, cap - deployed)


def append_order_log(state: dict, entry: dict) -> None:
    state.setdefault("order_log", []).append(entry)
    save_state(state)
