"""Autonomous loop scheduler. Cost-optimized: 3 cycles/day with role-specific agents."""
import argparse
import os
import resource
import time
import traceback
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


def _raise_fd_limit(target: int = 16384) -> None:
    """Raise the open-file limit before anything touches the network.

    Under launchd the soft RLIMIT_NOFILE is only 256. yfinance (threads=True,
    ~66 tickers) opens enough concurrent connections to exhaust that, and FD
    starvation surfaces as MISLEADING errors — sqlite "unable to open database
    file", curl getaddrinfo/DNS failures — before finally erroring plainly with
    Errno 24. (This, not the cache path, was the true cause of the 6/22 spurious
    AMD sell and the 6/23 no-trade crash.) macOS caps a process at
    kern.maxfilesperproc (61440), so a generous 16384 is safe and well clear of
    real usage. Idempotent; never lowers the limit; swallows any failure.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except Exception:
        pass


_raise_fd_limit()

from agents import broker, exits, publish, health
from agents.analytics import agent as analytics
from agents.content import agent as content
from agents.allocator import agent as allocator
from agents.research import agent as research
from agents.tracker import agent as tracker


ET = ZoneInfo("America/New_York")

# Per-cycle collector of agent failures (consumed by the health watchdog).
_STEP_FAILURES: list[dict] = []


def market_closed_today() -> bool:
    """True on weekends and market holidays (Alpaca calendar). When closed we
    skip the whole cycle — no trading and, importantly, no duplicate stale
    post. Prices don't move on a closed day, so posting would just re-publish
    the prior session's numbers as if they were new."""
    return not broker.is_trading_day()


# Read-only steps are safe to retry on a transient (network/data) failure.
# Order-PLACING steps (exits, allocator) are NOT retried — a retry after a
# partial submit could double-place. Their resilience comes from preflight
# (verify broker/data are healthy before we ever call them).
_RETRYABLE = {"research": 2, "tracker": 2, "publish": 2, "content": 1, "analytics": 1}


def step(name: str, fn):
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] >>> {name}")
    tries = _RETRYABLE.get(name, 0) + 1
    last_exc = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            print(f"[{name}] FAILED (attempt {attempt + 1}/{tries}): {e}")
            if attempt < tries - 1:
                time.sleep(3)
    traceback.print_exc()
    _STEP_FAILURES.append({"name": name, "error": str(last_exc)[:300]})
    return None


def cycle_premarket():
    """Once daily ~9:00 ET. Exits first (queue sells for the open), then find
    candidates + queue/place buys for the open."""
    step("exits", exits.run)
    step("research", research.run)
    step("tracker", tracker.run)
    step("allocator", allocator.run)
    step("publish", publish.run)


def cycle_midday():
    """Once daily ~12:00 ET. Intraday stop check + tracker refresh, then a short
    midday update post (today's trades, cash deployed, the reasoning). The extra
    ~$0.02/day (1 Haiku call + 1 plain X post) buys a second daily touchpoint."""
    step("exits", exits.run)
    step("tracker", tracker.run)
    step("midday_post", content.run_midday)
    step("publish", publish.run)


def cycle_postclose():
    """Once daily ~16:30 ET. Final tracker + the ONE daily X post + analytics."""
    step("tracker", tracker.run)
    step("content", content.run)  # generates and posts the 1 daily summary
    step("analytics", analytics.run)
    step("publish", publish.run)


CYCLES = {
    "premarket": (9, 0, cycle_premarket),
    "midday": (12, 0, cycle_midday),
    "postclose": (16, 30, cycle_postclose),
}


def run_cycle(name: str, fn) -> None:
    """Run one cycle wrapped by the health watchdog: preflight (env sanity +
    self-heal) -> agents -> postflight (did they do their job?) -> alert on
    anything unhealed. A fatal exception is caught and pushed, never silent."""
    _STEP_FAILURES.clear()
    started = datetime.now(timezone.utc).isoformat()
    try:
        pre = health.preflight()
        health.report(f"{name}/preflight", pre)
        # Abort trading if the environment is critically broken (e.g. broker
        # down, keys missing) — better to skip a cycle than trade blind.
        if any(i.severity == "critical" and not i.healed for i in pre):
            print(f"=== ABORT {name}: critical preflight failure, skipping agents ===")
            return
        fn()
        post = health.postflight(name, list(_STEP_FAILURES), started)
        health.report(name, post)
    except Exception as e:
        health.notify_crash(name, e, traceback.format_exc())
        raise


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
    parser.add_argument("--simulate", action="store_true",
                        help="Compute a full premarket cycle's decisions against REAL positions but submit NO orders and write NO state (sets SIMULATE=1)")
    parser.add_argument("--replay", "--dry", dest="replay", action="store_true",
                        help="Run all 3 cycles back-to-back for testing. WARNING: places REAL orders unless combined with --simulate. Uses tokens.")
    args = parser.parse_args()

    if args.simulate:
        os.environ["SIMULATE"] = "1"
        print(f"=== SIMULATE premarket @ {now_et().isoformat()} — NO orders will be submitted ===")
        cycle_premarket()
        return

    if args.once:
        if market_closed_today():
            print(f"=== SKIP {args.once} @ {now_et().isoformat()} — market closed (weekend/holiday) ===")
            return
        print(f"=== ONCE: {args.once} @ {now_et().isoformat()} ===")
        _, _, fn = CYCLES[args.once]
        run_cycle(args.once, fn)
        return

    if args.replay:
        print(f"=== REPLAY — all 3 cycles back-to-back ===")
        for name, (_, _, fn) in CYCLES.items():
            print(f"\n>>> Cycle: {name}")
            run_cycle(name, fn)
        return

    print(f"Runner started. Cycles at 9:00 / 12:00 / 16:30 ET.")
    while True:
        name, target, fn = next_cycle()
        wait = (target - now_et()).total_seconds()
        print(f"\nNext cycle: {name} at {target.isoformat()} (sleeping {int(wait/60)}m)")
        time.sleep(max(1, wait))
        try:
            if market_closed_today():
                print(f"\n=== SKIP {name} @ {now_et().isoformat()} — market closed (weekend/holiday) ===")
                continue
            print(f"\n=== RUNNING {name} @ {now_et().isoformat()} ===")
            run_cycle(name, fn)
        except Exception as e:
            print(f"Cycle {name} failed: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
