"""Autonomous loop scheduler. Cost-optimized: 3 cycles/day with role-specific agents."""
import argparse
import time
import traceback
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from agents import broker, publish
from agents.analytics import agent as analytics
from agents.content import agent as content
from agents.allocator import agent as allocator
from agents.research import agent as research
from agents.tracker import agent as tracker


ET = ZoneInfo("America/New_York")


def step(name: str, fn):
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] >>> {name}")
    try:
        return fn()
    except Exception as e:
        print(f"[{name}] FAILED: {e}")
        traceback.print_exc()
        return None


def cycle_premarket():
    """Once daily ~9:00 ET. Find candidates + queue/place orders for the open."""
    step("research", research.run)
    step("tracker", tracker.run)
    step("allocator", allocator.run)
    step("publish", publish.run)


def cycle_midday():
    """Once daily ~12:00 ET. Light: refresh tracker + draft a post."""
    step("tracker", tracker.run)
    step("content", content.run)
    step("publish", publish.run)


def cycle_postclose():
    """Once daily ~16:30 ET. Final tracker, content recap, analytics."""
    step("tracker", tracker.run)
    step("content", content.run)
    step("analytics", analytics.run)
    step("publish", publish.run)


CYCLES = {
    "premarket": (9, 0, cycle_premarket),
    "midday": (12, 0, cycle_midday),
    "postclose": (16, 30, cycle_postclose),
}


def now_et() -> datetime:
    return datetime.now(ET)


def next_cycle() -> tuple[str, datetime, callable]:
    n = now_et()
    candidates = []
    for name, (h, m, fn) in CYCLES.items():
        target = n.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= n:
            target = target + timedelta(days=1)
        candidates.append((target, name, fn))
    candidates.sort()
    target, name, fn = candidates[0]
    return name, target, fn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", choices=list(CYCLES.keys()), help="Run a single named cycle and exit")
    parser.add_argument("--dry", action="store_true", help="Run all 3 cycles back-to-back for testing (uses tokens)")
    args = parser.parse_args()

    if args.once:
        print(f"=== ONCE: {args.once} @ {now_et().isoformat()} ===")
        _, _, fn = CYCLES["premarket"] if args.once == "premarket" else CYCLES[args.once]
        # Find the right fn
        _, _, fn = next(((h, m, fn) for n, (h, m, fn) in CYCLES.items() if n == args.once), (None, None, None))
        if fn:
            fn()
        return

    if args.dry:
        print(f"=== DRY RUN — all 3 cycles back-to-back ===")
        for name, (_, _, fn) in CYCLES.items():
            print(f"\n>>> Cycle: {name}")
            fn()
        return

    print(f"Runner started. Cycles at 9:00 / 12:00 / 16:30 ET.")
    while True:
        name, target, fn = next_cycle()
        wait = (target - now_et()).total_seconds()
        print(f"\nNext cycle: {name} at {target.isoformat()} (sleeping {int(wait/60)}m)")
        time.sleep(max(1, wait))
        try:
            print(f"\n=== RUNNING {name} @ {now_et().isoformat()} ===")
            fn()
        except Exception as e:
            print(f"Cycle {name} failed: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
