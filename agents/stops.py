"""Broker-side protective stops. Free-tier, no LLM.

The cycle-based exit rules only look at 9:00 / 12:00 / 16:30 — a crash at 10:15
could turn a -15% stop into -25% before anyone looked. This module lays a
STOP-LOSS SELL at Alpaca for every position each cycle, so the floor is enforced
by the broker continuously during market hours.

Constraints that shape the design (probed live 2026-07-16):
  * Fractional orders must be DAY — GTC is rejected. So stops expire at each
    close and are RE-LAID every cycle. A DAY order submitted after hours is
    valid for the whole next session, so the postclose sync covers the next
    open and the premarket sync refreshes levels after exits/rotation.
  * Shares committed to an open sell can't be sold again — broker.submit_sell
    cancels open sells for the ticker first, and sync() skips tickers that
    already have a non-stop open sell (e.g. an exits market sell queued for
    the open).

The stop level mirrors exits.RULES as a FLOOR: the bucket hard stop, tightened
to the trailing level once the position's high-water-mark has armed it. The
cycle logic in exits.py remains authoritative for everything richer (time
stops, take-profit, trend-break, trims); this is gap insurance, not a
replacement. A fired stop is reconciled into order_log / realized_pnl /
lottery_deployed_total by the next sync() so the books stay true.
"""
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents import broker
from agents.exits import RULES, effective_hard_stop_pct
from agents._state import (
    append_order_log,
    atr_map_from_log,
    bucket_map_from_log,
    load_state,
    reduce_lottery_deployed,
    save_state,
    should_execute,
    simulating,
)

load_dotenv()


def protective_stop_price(bucket: str, entry_price: float, peak_pl_pct: float,
                          atr_pct: float | None = None) -> float | None:
    """The broker-side floor for a position: bucket hard stop vs entry (ATR-
    tightened when the entry-time ATR is known — mirrors exits exactly), raised
    to the trailing give-back level once the high-water-mark has armed it.
    Pure function (unit-tested). None for an unknown bucket."""
    rules = RULES.get(bucket)
    if not rules or entry_price <= 0:
        return None
    stop = entry_price * (1 + effective_hard_stop_pct(bucket, atr_pct) / 100.0)
    arm, trail = rules.get("trail_arm_pct"), rules.get("trail_pct")
    if arm is not None and trail is not None and peak_pl_pct >= arm:
        stop = max(stop, entry_price * (1 + (peak_pl_pct - trail) / 100.0))
    return round(stop, 2)


def _reconcile_fired(state: dict) -> list[dict]:
    """Settle the book for stops that TRIGGERED at the broker since last sync:
    record the sell in order_log, roll realized P/L, release lottery cost basis.
    Terminal-unfilled records (expired DAY stop, canceled) are simply dropped —
    the position is still held and gets a fresh stop below."""
    recs = state.get("protective_stops", {})
    fired = []
    for t, rec in list(recs.items()):
        info = broker.order_status(rec["order_id"])
        if info is None:
            continue  # API hiccup — keep the record, retry next sync
        if info["filled_qty"] > 0:
            realized = (info["filled_avg_price"] or rec["stop_price"]) * info["filled_qty"] \
                       - rec["entry_price"] * info["filled_qty"]
            bucket = rec.get("bucket", "core")
            state.setdefault("realized_pnl", {}).setdefault(bucket, 0.0)
            state["realized_pnl"][bucket] = round(state["realized_pnl"][bucket] + realized, 2)
            if bucket == "lottery":
                reduce_lottery_deployed(state, info["filled_qty"] * rec["entry_price"])
            append_order_log(state, {
                "ts": datetime.now(timezone.utc).isoformat(), "ticker": t,
                "bucket": bucket, "side": "sell", "shares": info["filled_qty"],
                "reason": f"broker-side stop triggered @ {rec['stop_price']}",
                "realized_pl_dollars": round(realized, 2),
            })
            fired.append({"ticker": t, "realized": round(realized, 2)})
            print(f"[stops] {t}: broker stop FIRED @ {rec['stop_price']} (P/L ${realized:+.2f})")
            del recs[t]
        elif info["terminal"]:
            del recs[t]  # expired/canceled without filling — will be re-laid
    state["protective_stops"] = recs
    return fired


def sync() -> dict:
    """Reconcile fired stops, then (re-)lay a DAY stop for every unprotected
    position. Safe to run every cycle: cancel-and-replace, never duplicates."""
    state = load_state()
    positions = broker.positions()
    buckets = bucket_map_from_log(state)
    atrs = atr_map_from_log(state)
    highs = state.get("position_highs", {})

    if simulating() or not should_execute(state):
        planned = []
        for pos in positions:
            sp = protective_stop_price(buckets.get(pos["ticker"], "core"),
                                       pos["entry_price"],
                                       highs.get(pos["ticker"], {}).get("high_pl_pct", 0.0),
                                       atr_pct=atrs.get(pos["ticker"]))
            if sp and sp < pos["current_price"]:
                planned.append({"ticker": pos["ticker"], "stop_price": sp})
                print(f"  [SIM STOP] {pos['ticker']:5s} stop @ ${sp:.2f} (now ${pos['current_price']:.2f})")
        return {"laid": [], "fired": [], "planned": planned}

    fired = _reconcile_fired(state)

    recs = state.get("protective_stops", {})
    held = {p["ticker"] for p in positions}
    # A protective-stop record for a ticker we no longer hold and that didn't
    # fire (e.g. the cycle exits sold it, canceling our stop) is just stale.
    for t in list(recs):
        if t not in held:
            broker.cancel_open_sells(t)
            del recs[t]

    # Tickers with a NON-stop open sell (exits/rotation market sell queued for
    # the open) — their shares are already leaving; don't fight over them.
    pending_sells = {o["ticker"] for o in broker.open_orders() if o["side"] == "sell"}

    laid = []
    for pos in positions:
        t = pos["ticker"]
        if t in pending_sells and t not in recs:
            continue
        bucket = buckets.get(t, "core")
        stop_price = protective_stop_price(
            bucket, pos["entry_price"], highs.get(t, {}).get("high_pl_pct", 0.0),
            atr_pct=atrs.get(t))
        if stop_price is None:
            continue
        # Already below the floor (overnight gap): a stop would trigger the
        # instant the market opens at an uncontrolled price — leave it to this
        # cycle's exits pass, which is about to market-sell it anyway.
        if stop_price >= pos["current_price"]:
            continue
        existing = recs.get(t)
        if existing and abs(existing.get("stop_price", 0) - stop_price) < 0.01 \
                and existing.get("shares") == pos["shares"]:
            # Same level, same size, order still open -> keep it (DAY orders die
            # at the close on their own; only re-lay when it's gone or stale).
            info = broker.order_status(existing["order_id"])
            if info is not None and not info["terminal"]:
                continue
        broker.cancel_open_sells(t)
        try:
            order = broker.submit_stop_sell(t, pos["shares"], stop_price)
        except Exception as e:
            print(f"[stops] {t}: could not lay stop @ {stop_price}: {str(e)[:150]}")
            continue
        recs[t] = {
            "order_id": order["order_id"], "stop_price": stop_price,
            "shares": pos["shares"], "entry_price": pos["entry_price"],
            "bucket": bucket, "placed_at": datetime.now(timezone.utc).isoformat(),
        }
        laid.append({"ticker": t, "stop_price": stop_price})
        print(f"[stops] {t} ({bucket}): stop laid @ ${stop_price:.2f} "
              f"(entry ${pos['entry_price']:.2f}, now ${pos['current_price']:.2f})")

    state["protective_stops"] = recs
    save_state(state)
    return {"laid": laid, "fired": fired}


if __name__ == "__main__":
    print(sync())
