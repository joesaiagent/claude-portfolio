"""Shared portfolio state helpers used by all agents."""
import json
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


def lottery_remaining_budget(state: dict) -> float:
    cap = state["allocation_targets"]["lottery"]["max_loss_dollars"]
    deployed = state.get("lottery_deployed_total", 0.0)
    return max(0.0, cap - deployed)


def append_order_log(state: dict, entry: dict) -> None:
    state.setdefault("order_log", []).append(entry)
    save_state(state)
