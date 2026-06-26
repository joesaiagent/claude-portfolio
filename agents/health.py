"""Deterministic health watchdog. No LLM, no cost, can't itself introduce a
trading bug. Wraps every cycle with three guarantees:

  preflight()  — before agents run: verify the ENVIRONMENT is sane (FD limit,
                 API keys, data dir writable, broker reachable, data layer
                 returning prices) and AUTO-HEAL what is safely fixable
                 (re-raise the FD limit; retry transient broker/data hiccups).
  postflight() — after agents run: verify they actually DID their job (no step
                 threw, state file is valid, watchlist built, idle cash got
                 deployed, the daily post went out).
  notify_crash() — on an otherwise-fatal exception: push an alert with a
                 diagnosis instead of dying silently.

Anything it can't safely auto-fix it escalates via agents.notify (push/email),
so a failure is never discovered from the account balance again.

Each check returns Issue(severity, where, detail, healed). Severities:
  'critical' — trading integrity at risk (broker down, keys missing, can't write
               state). 'warning' — degraded but not dangerous (data source flaky,
               post missing). 'info' — auto-healed, logged for the record.
"""
from __future__ import annotations

import json
import os
import resource
import time
from dataclasses import dataclass

from agents import broker, notify
from agents._screener import fetch_history
from agents._state import STATE_FILE, WATCHLIST_FILE, DATA_DIR

FD_MIN = 8192          # below this we consider the limit unsafe and re-raise
FD_TARGET = 16384
IDLE_CASH_ALERT = 25.0  # spendable cash left undeployed after a premarket cycle


@dataclass
class Issue:
    severity: str   # critical | warning | info
    where: str
    detail: str
    healed: bool = False

    def __str__(self) -> str:
        mark = "✓ healed" if self.healed else self.severity.upper()
        return f"[{mark}] {self.where}: {self.detail}"


def _retry(fn, tries: int = 3, delay: float = 2.0):
    """Run fn, retrying transient failures. Returns (result, error_or_None)."""
    err = None
    for i in range(tries):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001 — health must survive anything
            err = e
            if i < tries - 1:
                time.sleep(delay)
    return None, err


# --------------------------------------------------------------------------- #
# Pre-flight: environment sanity + safe auto-heal.
# --------------------------------------------------------------------------- #
def preflight() -> list[Issue]:
    issues: list[Issue] = []

    # 1. File-descriptor limit — the recurring crash. Re-raise it here too, so
    #    the watchdog self-heals even if the runner's early raise was bypassed.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < FD_MIN:
        want = FD_TARGET if hard == resource.RLIM_INFINITY else min(FD_TARGET, hard)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            issues.append(Issue("info", "fd_limit",
                                 f"raised soft limit {soft}->{want}", healed=True))
        except Exception as e:
            issues.append(Issue("critical", "fd_limit",
                                 f"soft limit {soft} too low and could not raise: {e}"))

    # 2. Required API keys.
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        if not os.getenv(key):
            issues.append(Issue("critical", "env", f"{key} missing"))

    # 3. Data dir writable (state/watchlist must persist).
    try:
        probe = DATA_DIR / ".health_probe"
        probe.write_text("ok")
        probe.unlink()
    except Exception as e:
        issues.append(Issue("critical", "data_dir", f"not writable: {e}"))

    # 4. Portfolio state loadable (load_state self-heals from .bak; only an
    #    unrecoverable read reaches here — that's a hard stop, never trade blind).
    from agents._state import load_state
    try:
        load_state()
    except Exception as e:
        issues.append(Issue("critical", "state_file", f"unrecoverable: {e}"))

    # 4. Broker reachable (retry transient outages — that IS the heal).
    acct, err = _retry(broker.account_info)
    if err:
        issues.append(Issue("critical", "broker", f"unreachable after retries: {err}"))
    elif acct:
        # cash sanity: negative/NaN would mean a corrupt read.
        if acct.get("equity", 0) <= 0:
            issues.append(Issue("warning", "broker", f"equity reads {acct.get('equity')}"))

    # 5. Data layer returning prices (retry — flaky source is transient).
    #    days=60: fetch_history drops any series under 30 rows, so a short window
    #    would always read empty and false-alarm.
    hist, err = _retry(lambda: fetch_history(["SPY"], days=60), tries=3, delay=3.0)
    if err or not hist or "SPY" not in hist:
        issues.append(Issue("warning", "data", "price fetch returned nothing for SPY "
                            "(degraded source; agents fail-safe but may under-trade)"))

    return issues


# --------------------------------------------------------------------------- #
# Post-flight: did the agents actually do their job?
# --------------------------------------------------------------------------- #
def _new_buys_this_cycle(started_iso: str) -> int:
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return 0
    return sum(1 for o in state.get("order_log", [])
               if o.get("side") == "buy" and o.get("ts", "") >= started_iso)


def postflight(cycle: str, step_failures: list[dict], started_iso: str) -> list[Issue]:
    issues: list[Issue] = []

    # 1. Any agent that threw is already a failure (collected by the runner).
    for f in step_failures:
        issues.append(Issue("critical", f"agent:{f['name']}", f["error"]))

    # 2. State file must be valid JSON (corruption = silent data loss).
    try:
        json.loads(STATE_FILE.read_text())
    except Exception as e:
        issues.append(Issue("critical", "state_file", f"invalid/unreadable: {e}"))

    # 3. Premarket-specific: watchlist built + idle cash deployed.
    if cycle == "premarket":
        try:
            wl = json.loads(WATCHLIST_FILE.read_text())
            if not wl:
                issues.append(Issue("warning", "watchlist", "empty after research"))
        except Exception as e:
            issues.append(Issue("warning", "watchlist", f"missing/unreadable: {e}"))

        # The deploy bug detector: spendable (settled) cash left idle with
        # nothing queued and nothing bought this cycle => allocation silently
        # failed to deploy (exactly the 6/23 failure).
        acct, err = _retry(broker.account_info)
        if not err and acct:
            spendable = acct.get("non_marginable_buying_power")
            if spendable is None:
                spendable = acct.get("buying_power", 0.0)
            try:
                open_buys = [o for o in broker.open_orders() if o["side"] == "buy"]
            except Exception:
                open_buys = []
            placed = _new_buys_this_cycle(started_iso)
            if spendable > IDLE_CASH_ALERT and not open_buys and placed == 0:
                issues.append(Issue("warning", "allocator",
                    f"${spendable:.0f} spendable cash left idle — no buys placed and "
                    f"no open orders after premarket (possible deploy failure)"))

    # 4. Postclose-specific: the daily post must have gone out.
    if cycle == "postclose":
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            from agents._state import DATA_DIR as _DD  # noqa
            import pathlib
            posts_path = pathlib.Path(_DD).parent / "docs" / "posts.json"
            posts = json.loads(posts_path.read_text())
            last = posts[-1] if posts else {}
            # The daily post counts as done if it actually went out today on ANY
            # platform — _emit sets status="posted", posted_at, and lists every
            # platform that delivered in posted_platforms. Missing X (no creds /
            # 403 dup) while Bluesky/Mastodon delivered is still a successful post,
            # so only alarm when nothing went anywhere for today's ET date.
            posted_today = (last.get("posted_at", "")[:10] == today
                            and last.get("status") == "posted"
                            and bool(last.get("posted_platforms")))
            if not posted_today:
                issues.append(Issue("warning", "content", "no post delivered for today"))
        except Exception as e:
            issues.append(Issue("warning", "content", f"could not verify daily post: {e}"))

    return issues


# --------------------------------------------------------------------------- #
# Escalation.
# --------------------------------------------------------------------------- #
def _alertable(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if not i.healed and i.severity in ("critical", "warning")]


def report(cycle: str, issues: list[Issue]) -> bool:
    """Log all issues; push an alert if any are unhealed. Returns True if alerted."""
    for i in issues:
        print(f"[health] {i}")
    bad = _alertable(issues)
    if not bad:
        if issues:  # only-healed: note it, no push
            print(f"[health] {cycle}: {len(issues)} issue(s) auto-healed, all clear")
        return False
    crit = sum(1 for i in bad if i.severity == "critical")
    level = "critical" if crit else "warning"
    title = f"{cycle} — {crit} critical, {len(bad) - crit} warning"
    body = f"claude-portfolio {cycle} health alert:\n\n" + "\n".join(f"• {i}" for i in bad)
    body += "\n\nCheck runner.log on the trading Mac."
    notify.send(title, body, level=level)
    return True


def notify_crash(cycle: str, exc: BaseException, tb: str) -> None:
    """Top-level fatal-exception handler: push a diagnosis instead of dying silent."""
    print(f"[health] FATAL in {cycle}: {exc}")
    body = (f"claude-portfolio {cycle} CRASHED:\n\n{type(exc).__name__}: {exc}\n\n"
            f"{tb[-1500:]}")
    notify.send(f"{cycle} CRASHED — {type(exc).__name__}", body, level="critical")
