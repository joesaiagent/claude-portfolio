"""Shared portfolio state helpers used by all agents."""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
STATE_FILE = DATA_DIR / "portfolio_state.json"
STATE_BAK = DATA_DIR / "portfolio_state.json.bak"   # last known-good snapshot
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
PENDING_TRADES_FILE = DATA_DIR / "pending_trades.json"
POSTS_FILE = DATA_DIR / "posts_queue.json"
TRACKER_REPORT = DATA_DIR / "tracker_report.json"

BUCKETS = ("core", "swing", "lottery")


def load_state() -> dict:
    """Load portfolio state, self-healing from the last-good backup if the
    primary file is missing or corrupt (e.g. a write interrupted by a process
    kill). Every agent calls this, so a truncated state file would otherwise
    crash every cycle until manually repaired."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, FileNotFoundError, ValueError) as e:
        if STATE_BAK.exists():
            data = json.loads(STATE_BAK.read_text())  # if THIS fails too, truly unrecoverable -> raise
            STATE_FILE.write_text(json.dumps(data, indent=2))
            print(f"[state] primary state unreadable ({e}); recovered from backup")
            return data
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Atomic write for any file: temp in the SAME dir + os.replace (atomic on
    POSIX, same filesystem), so a process kill mid-write can never leave a
    truncated file. Shared by agents that persist regenerable JSON (watchlist,
    tracker report, posts queue, pending trades) — mirrors save_state's idiom
    without the .bak snapshot (those files self-regenerate, state does not)."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def save_state(state: dict) -> None:
    """Atomic write: serialize to a temp file then os.replace() (atomic on
    POSIX) so an interrupted write can never leave a truncated/corrupt state.
    Snapshots the prior good file to .bak first, so load_state can self-heal."""
    payload = json.dumps(state, indent=2)
    tmp = DATA_DIR / (STATE_FILE.name + ".tmp")
    tmp.write_text(payload)
    if STATE_FILE.exists():
        shutil.copy2(STATE_FILE, STATE_BAK)
    os.replace(tmp, STATE_FILE)  # atomic rename


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


def atr_map_from_log(state: dict) -> dict[str, float]:
    """ticker -> atr_pct stamped on its most recent buy (the allocator records
    it at entry so stops stay anchored to entry-time volatility). Names bought
    before 2026-09-01 carry none — callers fall back to the bucket's flat stop,
    so legacy positions keep the risk contract they were opened under."""
    out: dict[str, float] = {}
    for e in state.get("order_log", []):
        if e.get("side") == "buy" and e.get("ticker") and e.get("atr_pct"):
            out[e["ticker"]] = float(e["atr_pct"])  # later buys overwrite
    return out


def lottery_remaining_budget(state: dict) -> float:
    cap = state["allocation_targets"]["lottery"]["max_loss_dollars"]
    deployed = state.get("lottery_deployed_total", 0.0)
    return max(0.0, cap - deployed)


def add_lottery_deployed(state: dict, cost_basis: float) -> None:
    """Raise cumulative lottery deployment when a lottery buy is placed."""
    cur = state.get("lottery_deployed_total", 0.0)
    state["lottery_deployed_total"] = round(cur + cost_basis, 2)


def reduce_lottery_deployed(state: dict, cost_basis: float) -> None:
    """Lower cumulative lottery deployment when a lottery position is sold, by the
    sold shares' cost basis. Without this, lottery_deployed_total is a one-way
    ratchet that counts gross dollars ever deployed (not live exposure), so normal
    churn silently freezes the bucket at the cap. Clamped at 0 so float drift or a
    partial fill can never drive it negative."""
    cur = state.get("lottery_deployed_total", 0.0)
    state["lottery_deployed_total"] = round(max(0.0, cur - cost_basis), 2)


def append_order_log(state: dict, entry: dict) -> None:
    state.setdefault("order_log", []).append(entry)
    save_state(state)
