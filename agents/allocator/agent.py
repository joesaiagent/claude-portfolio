"""Allocator: free-tier. Deterministic bucket-aware sizing. No LLM cost. Executes via Alpaca."""
import json
import math
import statistics
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents import broker
from agents._regime import current_regime
from agents._screener import THEME_OF
from agents.exits import ATR_STOP_MULT, RULES
from agents._state import (
    BUCKETS,
    PENDING_TRADES_FILE,
    WATCHLIST_FILE,
    add_lottery_deployed,
    append_order_log,
    atomic_write_text,
    buy_dates,
    is_autonomous,
    load_state,
    lottery_remaining_budget,
    reduce_lottery_deployed,
    save_state,
    should_execute,
    simulating,
)

load_dotenv()


# Max position counts per bucket. Core holds up to 4 names so the bucket budget
# can always be spread under the per-position account cap below (a single core
# name capped at ~20% of a ~$300 account is ~$60, and $210/$60 -> 4 names). Swing
# and lottery stay at 2 (their budgets fit under the cap in 2 names).
BUCKET_MAX_POSITIONS = {"core": 4, "swing": 2, "lottery": 2}

# Hard cap on any single position as a fraction of TOTAL account equity — the
# guardrail against the concentration that drove the book's whole loss (a single
# core name that grew to ~38% of the account, then stopped out for ~-$18). This
# is an account-level cap, distinct from the per-bucket SIZING_MAX_FRAC below:
# even a low-vol name that inverse-vol wants to over-fund can never exceed this.
MAX_POSITION_ACCOUNT_FRAC = 0.20

# Inverse-volatility sizing caps (as a fraction of a bucket's remaining budget),
# so a steady low-vol name gets more capital but no single name dominates a tiny
# account. Floors keep every position clear of Alpaca's $1 min notional.
SIZING_MIN_FRAC = {"core": 0.25, "swing": 0.30, "lottery": 0.45}
SIZING_MAX_FRAC = {"core": 0.55, "swing": 0.55, "lottery": 0.55}
VOL_FLOOR = 0.005

# ATR risk parity: cap each entry's dollars so a stop-out at the position's
# actual stop distance (ATR_STOP_MULT × entry ATR%, floored by the bucket hard
# stop — exits.effective_hard_stop_pct) loses ~RISK_PER_TRADE_FRAC of account
# equity. Equal-dollar sizing with a flat -15% stop let one high-ATR name (MU)
# risk 3× what a calm name did — one stop-out cost -$17.97, 6% of the account.
RISK_PER_TRADE_FRAC = 0.01

# Structured signal snapshot copied from the enriched candidate into each buy's
# order-log entry, so attribution can regress signals against forward returns
# without parsing thesis strings (pre-2026-09-01 buys need the regex fallback).
SIGNAL_FIELDS = (
    "score", "momentum_score", "news_sentiment", "finnhub_rec", "insider_mspr",
    "insider_transactions", "congressional", "options_pcr_signal",
    "analyst_upside_pct", "analyst_rec", "vol20", "atr_pct", "rsi14",
    "above_ma50", "conviction",
)

# Pyramiding (funded by the retired lottery sleeve): spare CORE budget adds to
# the strongest PROVEN winner instead of buying a fresh long-shot. Guardrails:
# only after +PYRAMID_MIN_GAIN_PCT (the add rides an already-armed trailing
# stop), one add per name per PYRAMID_COOLDOWN_DAYS (no compounding every
# cycle), and always under MAX_POSITION_ACCOUNT_FRAC like any other buy.
PYRAMID_MIN_GAIN_PCT = 15.0
PYRAMID_COOLDOWN_DAYS = 7
PYRAMID_MIN_DOLLARS = 5.0


def _atr_risk_cap(cand: dict, equity: float, bucket: str) -> float | None:
    """Max dollars for this entry so a stop-out loses ~RISK_PER_TRADE_FRAC of
    equity at the position's effective stop distance. None (no cap) when the
    candidate predates atr_pct in the watchlist — degrades to prior sizing."""
    atr = cand.get("atr_pct")
    if not atr or atr <= 0 or equity <= 0:
        return None
    stop_frac = min(ATR_STOP_MULT * atr, -RULES[bucket]["hard_stop_pct"]) / 100.0
    if stop_frac <= 0:
        return None
    return (RISK_PER_TRADE_FRAC * equity) / stop_frac


def _signals_from_candidate(cand: dict) -> dict:
    return {k: cand[k] for k in SIGNAL_FIELDS if cand.get(k) is not None}


def _inverse_vol_budgets(cands: list[dict], remaining: float, bucket: str,
                         max_dollars: float | None = None) -> list[float]:
    """Dollar budget per candidate via inverse-vol weighting, clamped to per-bucket
    min/max fractions and water-fill renormalized to sum to `remaining`.

    `max_dollars` (the account-level per-position cap) tightens the upper bound so
    no single name exceeds it. If the cap is so tight that n names can't absorb
    `remaining` (n * max_dollars < remaining), water-fill deploys what it can and
    leaves the remainder as cash — conservative, never over-concentrates."""
    n = len(cands)
    if n == 0:
        return []
    if n == 1:
        return [remaining if max_dollars is None else min(remaining, max_dollars)]
    inv = [1.0 / max(c.get("vol20") or 0.02, VOL_FLOOR) for c in cands]
    tot = sum(inv)
    budgets = [remaining * x / tot for x in inv]
    # Effective caps must straddle the equal-weight share (1/n) so full deployment
    # is always feasible — otherwise with few names the static caps would leave
    # part of the bucket undeployed (e.g. 2 core names capped at 0.35 = only 70%).
    lo = min(SIZING_MIN_FRAC.get(bucket, 0.0), 1.0 / n) * remaining
    hi = max(SIZING_MAX_FRAC.get(bucket, 1.0), 1.0 / n) * remaining
    if max_dollars is not None:
        hi = min(hi, max_dollars)
        lo = min(lo, hi)  # a tight account cap can push hi below the usual floor
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


def _latest_buy_dates(state: dict) -> dict[str, datetime]:
    """MOST RECENT buy timestamp per ticker (buy_dates gives the earliest —
    right for hold-time rules, wrong for the pyramid cooldown, which must key
    off the last add or it would re-add every cycle)."""
    out: dict[str, datetime] = {}
    for e in state.get("order_log", []):
        if e.get("side") == "buy" and e.get("ticker") and e.get("ts"):
            ts = datetime.fromisoformat(e["ts"])
            if e["ticker"] not in out or ts > out[e["ticker"]]:
                out[e["ticker"]] = ts
    return out


def _themes_held(tickers) -> set[str]:
    """Theme labels represented in a set of tickers (unthemed names contribute none)."""
    return {THEME_OF[t] for t in tickers if t in THEME_OF}


def _pick_candidates(bucket_candidates: list[dict], n_pos: int, used_themes: set[str],
                     price_lookup) -> list[tuple[dict, float]]:
    """Walk the sorted candidates and return up to n_pos (candidate, price) pairs.

    Skips any candidate whose THEME_OF cluster is already represented — either by
    a held position (used_themes at call time) or by an earlier pick this cycle —
    so a bucket can never hold two names from one correlated theme (the 7/16
    3-BTC-miner lottery book). A candidate that can't be priced just falls
    through to the next-best name (the 6/23 stranded-budget fix, preserved).
    Mutates used_themes with the themes of the picks so the caller's later
    buckets see them too."""
    priced: list[tuple[dict, float]] = []
    for c in bucket_candidates:
        theme = THEME_OF.get(c["ticker"])
        if theme and theme in used_themes:
            continue
        price = price_lookup(c)
        if price and price > 0:
            priced.append((c, price))
            if theme:
                used_themes.add(theme)
        if len(priced) >= n_pos:
            break
    return priced


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
ROTATION_DAILY_CAP = 3  # max rotations per bucket per day
ROTATION_WINNER_SHIELD_PCT = 5.0  # positions up >= this % are never rotated out
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
        remaining_cap = ROTATION_DAILY_CAP - rot.get(bucket, 0)
        if remaining_cap <= 0:
            continue
        cands = [c for c in watchlist if c.get("bucket") == bucket]
        if not cands:
            continue
        # Score held names from the fresh watchlist; unknowns fall back to the
        # bucket's candidate median (conservative — won't force a rotation).
        median = statistics.median([c.get("score", 50.0) for c in cands])
        def hscore(t):
            return score_by_ticker.get(t, median)

        queued_sells: set[str] = set()
        queued_buys: set[str] = set()
        for _ in range(remaining_cap):
            held_available = [p for p in held if p["ticker"] not in queued_sells]
            if len(held_available) < BUCKET_MAX_POSITIONS[bucket]:
                break  # below target count, stop rotating
            cands_available = [
                c for c in cands
                if c["ticker"] not in {p["ticker"] for p in held_available}
                and c["ticker"] not in queued_buys
                and c["ticker"] not in queued_sells
            ]
            if not cands_available:
                break
            best = max(cands_available, key=lambda c: c.get("score", 0.0))
            rotatable = [p for p in held_available
                         if p.get("pl_pct", 0.0) < ROTATION_WINNER_SHIELD_PCT]
            if not rotatable:
                break  # all held positions are winners — don't force a sell
            weakest = min(rotatable, key=lambda p: hscore(p["ticker"]))
            # Theme cap: the buy must not share a correlated theme with anything
            # that REMAINS after the sell (held minus weakest, plus queued buys).
            # Conservative on collision: stop rotating this bucket for the cycle.
            best_theme = THEME_OF.get(best["ticker"])
            remaining_themes = _themes_held(
                [p["ticker"] for p in held_available if p["ticker"] != weakest["ticker"]]
            ) | _themes_held(queued_buys)
            if best_theme and best_theme in remaining_themes:
                break
            margin = best.get("score", 0.0) - hscore(weakest["ticker"])
            if margin < ROTATE_MARGIN[bucket]:
                break
            held_days = (now - dates[weakest["ticker"]]).total_seconds() / 86400 if weakest["ticker"] in dates else 0.0
            if held_days < ROTATE_MIN_HOLD.get(bucket, 9999.0):
                break
            actions.append({"bucket": bucket, "sell": weakest, "buy": best, "margin": round(margin, 2)})
            rot[bucket] = rot.get(bucket, 0) + 1
            queued_sells.add(weakest["ticker"])
            queued_buys.add(best["ticker"])

    return actions, rot


# Recorded buy statuses that mean "not yet known to have settled" — these get
# re-queried against the broker each cycle until they reach a terminal state.
# (Statuses are stored as str(OrderStatus.X), e.g. "OrderStatus.ACCEPTED".)
def _status_is_terminal(status: str | None) -> bool:
    if not status:
        return False
    return status.split(".")[-1].lower() in broker.TERMINAL_ORDER_STATUSES


def reconcile_order_log(state: dict) -> int:
    """Reconcile recent non-terminal buy orders in the log against the broker's
    actual fills. MUST run early in the cycle, before any sizing/rotation helper
    reads order_log — an unfilled/expired DAY limit otherwise lingers as a phantom
    holding and corrupts bucket_map_from_log / buy_dates / bucket_remaining_budget.

    For each buy entry whose recorded status is still non-terminal:
      * any fill (filled_qty > 0) -> a real holding: record filled_qty/
                             filled_avg_price and KEEP side=='buy'. A DAY order can
                             go terminal as done_for_day/canceled/expired while
                             still carrying a partial fill — those shares are held
                             and must stay visible to bucket_map_from_log /
                             buy_dates / _attach_bucket (else exits never sell them).
                             For a lottery buy, also correct lottery_deployed_total
                             from the estimate (shares*max_price ceiling) to the
                             ACTUAL filled basis, so the deployed total is symmetric
                             with the sell side (which reduces shares*entry_price).
      * zero fill (filled_qty == 0) -> flip side to 'buy_unfilled' so
                             bucket_map_from_log, buy_dates, and _attach_bucket
                             (which all gate on side=='buy') stop treating it as a
                             live holding, and revert the full lottery cost basis
                             via reduce_lottery_deployed.
    The lottery correction is idempotent: the loop skips entries whose recorded
    status is already terminal (_status_is_terminal), and the status is set
    terminal in the SAME pass that applies the correction, so a re-run can't
    double-apply it.
    Returns the number of entries changed; persists via save_state if anything did.
    """
    changed = 0
    for e in state.get("order_log", []):
        if e.get("side") != "buy" or not e.get("order_id"):
            continue
        if _status_is_terminal(e.get("status")):
            continue
        info = broker.order_status(e["order_id"])
        if info is None or not info["terminal"]:
            continue  # API hiccup or still working — leave it for a later cycle
        e["status"] = info["status"]
        e["reconciled"] = True
        if info["filled_qty"] > 0:
            # Any fill (full OR a terminal partial) is a real holding — keep
            # side=='buy' so exits/attribution still see it.
            e["filled_qty"] = info["filled_qty"]
            e["filled_avg_price"] = info["filled_avg_price"]
            if e.get("bucket") == "lottery":
                # Correct lottery_deployed_total from the limit-ceiling estimate to
                # the actual filled basis so round-trips don't ratchet the total up
                # (the sell side reduces shares*entry_price = actual basis). Fires
                # once: the loop skips already-terminal entries on a re-run.
                actual_basis = info["filled_qty"] * (info["filled_avg_price"] or 0.0)
                delta = actual_basis - e.get("estimated_cost", 0.0)
                if delta > 0:
                    add_lottery_deployed(state, delta)
                elif delta < 0:
                    reduce_lottery_deployed(state, -delta)
        else:
            # Terminal with zero fill: stop downstream helpers from counting it.
            e["side"] = "buy_unfilled"
            if e.get("bucket") == "lottery":
                reduce_lottery_deployed(state, e.get("estimated_cost", 0.0))
        changed += 1
    if changed:
        save_state(state)
    return changed


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

    # Reconcile the order log against actual broker fills FIRST, before any helper
    # below reads order_log for sizing/attribution. An unfilled/expired DAY limit
    # left at its submission-time status is a phantom holding that corrupts
    # bucket_map_from_log, buy_dates and bucket_remaining_budget. Skip in SIMULATE
    # (no live broker calls) — there are no real orders to reconcile there anyway.
    if should_execute(state):
        reconcile_order_log(state)
        state = load_state()

    # research.run() writes the watchlist non-atomically, so a truncated read here
    # would raise mid-write. Degrade to an empty watchlist (no buys this cycle)
    # instead of killing the non-retryable buy phase.
    try:
        watchlist = json.loads(WATCHLIST_FILE.read_text()) if WATCHLIST_FILE.exists() else []
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"[allocator] watchlist unreadable ({e}); treating as empty")
        watchlist = []

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
    # Account equity anchors the per-position hard cap (MAX_POSITION_ACCOUNT_FRAC).
    # Fall back through equity/cash if portfolio_value is missing so the cap is
    # never silently disabled (which would re-open the concentration hole).
    equity = account.get("portfolio_value") or account.get("equity") or account.get("cash") or 0.0
    max_per_pos = MAX_POSITION_ACCOUNT_FRAC * equity if equity > 0 else None
    positions = _attach_bucket(broker.positions(), state)
    # Net out still-open buy orders (e.g. a prior cycle's DAY limit not yet
    # filled): treat those tickers as already taken AND reserve their cash, so a
    # later cycle in the same day never double-buys or over-spends. Alpaca's
    # `cash` field doesn't drop until fill, so without this the cap is blind to
    # pending orders. Idempotent allocation is essential for an unattended loop.
    open_buys = [o for o in broker.open_orders() if o["side"] == "buy"]
    open_buy_tickers = {o["ticker"] for o in open_buys}
    reserved_cash = sum(o["notional"] for o in open_buys)
    held_tickers = {p["ticker"] for p in positions} | open_buy_tickers
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
        # Spread across enough names that the bucket budget can fully deploy
        # WITHOUT any single position breaching the account-level cap. Without
        # this, a bucket with a large remaining budget but a low base position
        # count (e.g. remaining $114 -> base 1 name) dumped the whole budget into
        # one name — exactly the 38%-of-account MU bet that stopped out. Bounded
        # by the bucket's max positions and (later) the count of priceable names.
        if max_per_pos and max_per_pos > 0:
            n_pos = max(n_pos, math.ceil(remaining / max_per_pos))
        # Cap NEW buys by the bucket's open slots (max positions minus what's
        # already held), so adding to a partly-filled bucket can't push it past
        # BUCKET_MAX_POSITIONS. A bucket already at its max is left to rotation.
        held_in_bucket = sum(1 for p in positions if p.get("bucket") == bucket)
        open_slots = BUCKET_MAX_POSITIONS[bucket] - held_in_bucket
        if open_slots <= 0:
            continue
        n_pos = min(n_pos, open_slots)
        # Resolve a tradeable price for each candidate UP FRONT (watchlist
        # last_price first, else Alpaca's reliable quote) and keep only the ones
        # we can actually price, walking down the sorted list until we have n_pos,
        # skipping candidates whose correlated theme (THEME_OF) is already held
        # in this bucket or reserved by an open buy. Seed used_themes fresh per
        # bucket: the cap is per-bucket, but open buys have no bucket info so
        # they block conservatively everywhere.
        used_themes = _themes_held(
            [p["ticker"] for p in positions if p.get("bucket") == bucket]
        ) | _themes_held(open_buy_tickers)
        priced = _pick_candidates(
            bucket_candidates, n_pos, used_themes,
            lambda c: c.get("last_price") or broker.latest_price(c["ticker"]),
        )
        if not priced:
            continue
        chosen = [c for c, _ in priced]
        price_by_ticker = {c["ticker"]: p for c, p in priced}
        # Inverse-vol sizing needs vol20 on every candidate; a stale watchlist
        # without it degrades safely to equal weight.
        if all(c.get("vol20") for c in chosen):
            budgets = _inverse_vol_budgets(chosen, remaining, bucket, max_per_pos)
        else:
            equal = remaining / len(chosen)
            if max_per_pos:
                equal = min(equal, max_per_pos)  # cap the equal-weight fallback too
            budgets = [equal] * len(chosen)
        # ATR risk-parity cap on top: a high-ATR name whose stop sits far from
        # entry gets fewer dollars, so every position risks the same ~1% of
        # equity at its stop. Trimmed dollars stay cash (never redistributed
        # onto other names — that would just re-concentrate the risk).
        capped_budgets = []
        for cand, budget in zip(chosen, budgets):
            cap = _atr_risk_cap(cand, equity, bucket)
            if cap is not None and cap < budget:
                print(f"[allocator] {cand['ticker']}: ATR risk cap trims "
                      f"${budget:.2f} -> ${cap:.2f} (ATR {cand['atr_pct']:.1f}%)")
                budget = cap
            capped_budgets.append(budget)
        budgets = capped_budgets
        for cand, budget in zip(chosen, budgets):
            price = price_by_ticker[cand["ticker"]]
            if budget < 1.0:
                continue
            # Floor (not round) to 4 dp so est_cost can NEVER exceed `budget`.
            # Rounding UP pushed a full-budget single-name core buy a few cents
            # over its bucket cap, and the strict `>` hard-cap check below then
            # rejected the WHOLE position — leaving core cash idle every cycle
            # (the 6/23 "$113 undeployed" bug). Flooring keeps it just under cap.
            shares = math.floor((budget / price) * 1e4) / 1e4
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
                "atr_pct": cand.get("atr_pct"),
                "signals": _signals_from_candidate(cand),
            })
            held_tickers.add(cand["ticker"])

    # --- Pyramid: spare CORE budget (freed by the retired lottery sleeve, or by
    # an exit) adds to the strongest PROVEN winner rather than a fresh name.
    # "Add to NOW at +15% beats a fresh WULF": the add rides a trailing stop
    # that's already armed, so its downside is the give-back, not a fresh -15%.
    planned_core = sum(s["estimated_cost"] for s in suggestions if s["bucket"] == "core")
    spare = bucket_remaining_budget(state, positions, "core", exposure) - planned_core
    if spare >= PYRAMID_MIN_DOLLARS:
        last_buys = _latest_buy_dates(state)
        now_utc = datetime.now(timezone.utc)
        winners = [
            p for p in positions
            if p.get("bucket") == "core"
            and p.get("pl_pct", 0.0) >= PYRAMID_MIN_GAIN_PCT
            and p["ticker"] not in open_buy_tickers
            and (p["ticker"] not in last_buys
                 or (now_utc - last_buys[p["ticker"]]).days >= PYRAMID_COOLDOWN_DAYS)
        ]
        if winners:
            best = max(winners, key=lambda p: p.get("pl_pct", 0.0))
            price = broker.latest_price(best["ticker"]) or best.get("current_price")
            add = spare
            if max_per_pos:
                # Account-level cap counts what's already in the position.
                add = min(add, max_per_pos - best.get("market_value", 0.0))
            if price and price > 0 and add >= PYRAMID_MIN_DOLLARS:
                shares = math.floor((add / price) * 1e4) / 1e4
                est_cost = round(shares * price, 2)
                if shares > 0 and est_cost >= 1.0:
                    suggestions.append({
                        "ticker": best["ticker"],
                        "bucket": "core",
                        "shares": shares,
                        "max_price": round(price * 1.02, 2),
                        "estimated_cost": est_cost,
                        "rationale": f"pyramid: adding to winner at {best['pl_pct']:+.1f}%",
                        "signals": {"pyramid": True,
                                    "pl_pct_at_add": round(best.get("pl_pct", 0.0), 2)},
                    })
                    print(f"[allocator] pyramid: +${est_cost:.2f} {best['ticker']} "
                          f"(winner {best['pl_pct']:+.1f}%, spare core ${spare:.2f})")

    # Hard cap: don't exceed cash or per-bucket budgets. Subtract cash already
    # reserved by still-open buy orders so concurrent/same-day cycles can't
    # collectively over-spend.
    safe = []
    spent_per_bucket = {b: 0.0 for b in BUCKETS}
    spent_total = 0.0
    cash = max(0.0, account["cash"] - reserved_cash)
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
                    "atr_pct": s.get("atr_pct"),
                    "signals": s.get("signals", {}),
                }
                if s["bucket"] == "lottery":
                    add_lottery_deployed(state, s["estimated_cost"])
                append_order_log(state, entry)
                state = load_state()
                placed.append(entry)
            except Exception as e:
                placed.append({"ticker": s["ticker"], "error": str(e)})
        atomic_write_text(PENDING_TRADES_FILE, "[]")
    elif simulating():
        print(f"=== SIMULATE: {len(safe)} buy order(s) NOT submitted ===")
        for s in safe:
            print(
                f"  [SIM BUY] [{s['bucket']:7s}] {s['ticker']:5s} x{s['shares']} "
                f"@ <=${s['max_price']:.2f} (~${s['estimated_cost']:.2f}) — {s['rationale'][:60]}"
            )
    else:
        atomic_write_text(PENDING_TRADES_FILE, json.dumps(safe, indent=2))

    # --- Rotation: sell the weakest holding in a full bucket to fund a much
    # stronger fresh name. Runs after exits (earlier in the cycle) have already
    # pruned, so it only ever considers survivors. Sells now; queues the buy.
    rotations = []
    actions, rot = _rotation_candidates(state, watchlist, positions)
    for a in actions:
        weak, buy, bucket = a["sell"], a["buy"], a["bucket"]
        info = {"bucket": bucket, "sell": weak["ticker"], "buy": buy["ticker"], "margin": a["margin"]}
        if should_execute(state):
            # Persist the rotation-count increment BEFORE attempting the sell, and
            # independent of its outcome: a rotation slot is consumed the moment we
            # try it. Otherwise a submit_sell exception would drop the increment and
            # the ROTATION_DAILY_CAP churn guard would silently reset, letting
            # same-day retries exceed the cap.
            state = load_state()
            state["rotations_today"] = rot
            save_state(state)
            try:
                order = broker.submit_sell(weak["ticker"], weak["shares"])
                state = load_state()
                realized = weak.get("pl_dollars", 0.0)
                state.setdefault("realized_pnl", {}).setdefault(bucket, 0.0)
                state["realized_pnl"][bucket] = round(state["realized_pnl"][bucket] + realized, 2)
                if bucket == "lottery":
                    reduce_lottery_deployed(state, weak["shares"] * weak.get("entry_price", 0.0))
                state["rotations_today"] = rot
                if not ROTATION_SAME_CYCLE_BUY:
                    queued = {k: buy.get(k) for k in
                              ("ticker", "bucket", "score", "conviction", "last_price",
                               "vol20", "atr_pct", "rsi14", "above_ma50", "thesis")}
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
