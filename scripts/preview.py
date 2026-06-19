"""Preview what the allocator WOULD buy next cycle — read-only, places no orders.

Rebuilds the watchlist (new universe + all data sources), then runs the live
allocator selection + inverse-vol sizing against the current portfolio. Pass
tickers to --sold to simulate positions that are being divested (e.g. queued
sells that fill at the next open).

  .venv/bin/python scripts/preview.py --sold LLY UNH
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents import broker
from agents.allocator import agent as alloc
from agents.research import agent as research
from agents._state import WATCHLIST_FILE, load_state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sold", nargs="*", default=[], help="tickers to treat as already sold")
    ap.add_argument("--no-refresh", action="store_true", help="reuse existing watchlist.json")
    args = ap.parse_args()

    if not args.no_refresh:
        print("Rebuilding watchlist (new universe + news/analyst/Finnhub)...")
        research.run()

    state = load_state()
    watchlist = json.loads(WATCHLIST_FILE.read_text())
    positions = alloc._attach_bucket(broker.positions(), state)
    positions = [p for p in positions if p["ticker"] not in set(args.sold)]  # simulate divestment
    held = {p["ticker"] for p in positions}

    print(f"\nHoldings after divesting {args.sold or '—'}: "
          f"{', '.join(sorted(held)) or '(none)'}\n")

    for bucket in alloc.BUCKETS:
        remaining = alloc.bucket_remaining_budget(state, positions, bucket)
        if remaining < 1.0:
            print(f"[{bucket}] full — no buys")
            continue
        cands = [c for c in watchlist if c.get("bucket") == bucket and c["ticker"] not in held]
        cands.sort(key=lambda c: (c.get("conviction", 0), c.get("score", 0)), reverse=True)
        n = alloc._bucket_target_positions(bucket, remaining)
        chosen = cands[:n]
        if chosen and all(c.get("vol20") for c in chosen):
            budgets = alloc._inverse_vol_budgets(chosen, remaining, bucket)
        else:
            budgets = [remaining / len(chosen)] * len(chosen) if chosen else []
        print(f"[{bucket}] ~${remaining:.0f} to deploy across {len(chosen)} name(s):")
        for c, b in zip(chosen, budgets):
            px = c["last_price"]
            sh = round(b / px, 4) if px else 0
            print(f"   BUY {c['ticker']:5s} ~${b:5.0f}  ({sh} sh @ ${px:.2f})  "
                  f"score={c['score']:.1f} conv={c['conviction']}")
            print(f"        {c.get('thesis','')[:72]}")
    print("\n(Preview only — autonomous_mode is off, so Monday this is written to "
          "pending_trades.json for review, not executed.)")


if __name__ == "__main__":
    main()
