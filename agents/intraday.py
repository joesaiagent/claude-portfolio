"""Intraday check: free-tier, no LLM. Real-time IEX last trades for every held
position, folded into position_highs so trailing/protective stop levels arm and
TIGHTEN on intraday peaks instead of waiting for the next tracker cycle.

Why it matters: the tracker only marks highs at 9:00 / 12:00 / 16:30 ET. A name
that spikes +18% at 10:40 and fades by noon never armed its trailing stop under
the old cadence — the give-back was measured from a peak the book never saw.
This runs at the start of the midday cycle, BEFORE exits and stops.sync, so
both see the freshest high-water-marks the free IEX feed can provide.

Also alarms (print + report) on any position already trading below its
protective stop level — the broker-side stop should have fired; if it didn't
(never laid, expired DAY order), the midday exits pass is about to handle it,
and the alarm makes the gap visible in the cycle log.
"""
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents import broker
from agents.stops import protective_stop_price
from agents._state import atr_map_from_log, bucket_map_from_log, load_state, save_state, simulating
from agents.tracker.agent import update_position_highs

load_dotenv()


def run() -> dict:
    state = load_state()
    positions = broker.positions()
    if not positions:
        return {"note": "no positions", "raised": 0, "breaches": []}

    prices = broker.latest_trades([p["ticker"] for p in positions])
    # Prefer the real-time IEX trade; fall back to the position's own mark.
    merged = [{**p, "current_price": prices.get(p["ticker"], p["current_price"])}
              for p in positions]

    prior = {t: h.get("high_price", 0.0)
             for t, h in state.get("position_highs", {}).items()}
    highs = update_position_highs(merged, state)
    raised = [t for t, h in highs.items() if h["high_price"] > prior.get(t, 0.0) + 1e-9]

    buckets = bucket_map_from_log(state)
    atrs = atr_map_from_log(state)
    breaches = []
    for pos in merged:
        t = pos["ticker"]
        stop = protective_stop_price(buckets.get(t, "core"), pos["entry_price"],
                                     highs.get(t, {}).get("high_pl_pct", 0.0),
                                     atr_pct=atrs.get(t))
        if stop is not None and pos["current_price"] < stop:
            breaches.append({"ticker": t, "price": pos["current_price"], "stop": stop})
            print(f"[intraday] ⚠ {t} trading ${pos['current_price']:.2f} < stop ${stop:.2f} "
                  f"— broker stop should have fired; exits pass will handle it")

    if raised:
        print(f"[intraday] raised high-water-mark: {', '.join(raised)}")
    if not simulating():
        save_state(state)  # persist the refreshed highs

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "checked": len(merged),
        "realtime_quotes": len(prices),
        "raised": raised,
        "breaches": breaches,
    }


if __name__ == "__main__":
    print(run())
