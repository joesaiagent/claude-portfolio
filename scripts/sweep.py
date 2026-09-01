"""Parameter sweep: grid-search core exit levels (hard stop / trail arm / trail
give-back) against the walk-forward backtest engine, so exits.RULES numbers are
chosen systematically instead of hand-tuned after each loss.

History is fetched ONCE, then every combo re-simulates over the same path.
Results rank by Sharpe (return per unit of pain), with total return and max
drawdown alongside — pick from the top cluster, not the single winner (a lone
outlier on one historical path is curve-fitting, a plateau of neighbors is a
robust region). The row matching the LIVE config in exits.RULES is marked <-.

Run:  .venv/bin/python scripts/sweep.py [--regime] [--top 15]
Caveats: core bucket only, daily closes, weekly rebalance, one historical path,
no fees/slippage. Directionally useful; not a forecast.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents._screener import CORE_UNIVERSE, fetch_history
from agents.exits import RULES
from backtest import LOOKBACK_DAYS, WARMUP, _metrics, _simulate

# Grid: bracket the live values (-15 / 15 / 25) with tighter and looser
# neighbors in every dimension. 4×3×4 = 48 combos, a few minutes on one core.
HARD_STOPS = (-10.0, -12.0, -15.0, -18.0)
TRAIL_ARMS = (10.0, 15.0, 20.0)
TRAILS = (15.0, 20.0, 25.0, 30.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regime", action="store_true",
                    help="apply the regime exposure filter inside every run")
    ap.add_argument("--top", type=int, default=15, help="rows to print")
    args = ap.parse_args()

    print(f"Fetching ~{LOOKBACK_DAYS}d history for {len(CORE_UNIVERSE)} core tickers...")
    hist = fetch_history(CORE_UNIVERSE, days=LOOKBACK_DAYS)
    if "SPY" not in hist:
        print("No SPY data — aborting.")
        return
    dates = hist["SPY"].index
    tickers = [t for t in CORE_UNIVERSE if t in hist and t != "SPY"]
    for t in tickers + ["SPY"]:
        hist[t] = hist[t].reindex(dates).ffill()
    spy_close = hist["SPY"]["Close"]
    vix = None
    if args.regime:
        try:
            vix = yf.download("^VIX", period="2y", progress=False,
                              auto_adjust=True)["Close"].reindex(dates).ffill()
            if isinstance(vix, pd.DataFrame):
                vix = vix.iloc[:, 0]
        except Exception:
            pass

    live = (RULES["core"]["hard_stop_pct"], RULES["core"]["trail_arm_pct"],
            RULES["core"]["trail_pct"])
    combos = [(hs, arm, tr) for hs in HARD_STOPS for arm in TRAIL_ARMS for tr in TRAILS]
    print(f"Sweeping {len(combos)} combos over {dates[WARMUP].date()} -> {dates[-1].date()} "
          f"(regime={'on' if args.regime else 'off'})...\n")

    rows = []
    for n, (hs, arm, tr) in enumerate(combos, 1):
        params = {"hard_stop": hs, "trail_arm": arm, "trail": tr}
        curve = _simulate("new", hist, dates, spy_close, tickers,
                          regime=args.regime, vix=vix, params=params)
        m = _metrics(curve)
        rows.append({"hard_stop": hs, "trail_arm": arm, "trail": tr, **m})
        print(f"  [{n:2d}/{len(combos)}] stop {hs:+.0f} arm {arm:.0f} trail {tr:.0f} "
              f"-> ret {m['total_return_pct']:+6.1f}%  DD {m['max_drawdown_pct']:6.1f}%  "
              f"Sharpe {m['sharpe']:5.2f}")

    rows.sort(key=lambda r: r["sharpe"], reverse=True)
    spy_ret = (spy_close.iloc[-1] / spy_close.iloc[WARMUP] - 1) * 100

    print(f"\n{'rank':<5}{'stop':>6}{'arm':>6}{'trail':>7}{'ret':>9}{'maxDD':>9}{'Sharpe':>8}")
    print("-" * 50)
    for i, r in enumerate(rows[:args.top], 1):
        mark = "  <- live" if (r["hard_stop"], r["trail_arm"], r["trail"]) == live else ""
        print(f"{i:<5}{r['hard_stop']:>6.0f}{r['trail_arm']:>6.0f}{r['trail']:>7.0f}"
              f"{r['total_return_pct']:>8.1f}%{r['max_drawdown_pct']:>8.1f}%"
              f"{r['sharpe']:>8.2f}{mark}")
    live_row = next((r for r in rows if (r["hard_stop"], r["trail_arm"], r["trail"]) == live), None)
    if live_row:
        print(f"\nLive config rank: {rows.index(live_row) + 1}/{len(rows)} "
              f"(ret {live_row['total_return_pct']:+.1f}%, DD {live_row['max_drawdown_pct']:.1f}%, "
              f"Sharpe {live_row['sharpe']:.2f})")
    print(f"SPY buy-and-hold over the window: {spy_ret:+.1f}%")
    print("\nPick from the top CLUSTER, not the single best row — a plateau of "
          "neighboring configs is robust; a lone spike is curve-fit.")


if __name__ == "__main__":
    main()
