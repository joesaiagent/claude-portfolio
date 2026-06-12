"""Research: free-tier multi-source. yfinance momentum screen, then enrich the top
picks with Alpha Vantage news sentiment + analyst consensus. No LLM cost."""
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents._intel import enrich
from agents._screener import screen_bucket
from agents._state import WATCHLIST_FILE

load_dotenv()


def run(buckets: tuple[str, ...] = ("core", "swing", "lottery")) -> dict:
    """Rebuild the watchlist fresh every run. (Previously skipped re-screening once
    the list hit 15 entries, so the allocator bought off multi-day-old scores.)"""
    watchlist = []
    for bucket in buckets:
        try:
            ranked = screen_bucket(bucket, top_n=5)
        except Exception as e:
            print(f"[research] {bucket} screening failed: {e}")
            continue
        ranked = enrich(ranked)  # news sentiment + analyst consensus, re-ranks
        now = datetime.now(timezone.utc).isoformat()
        watchlist.extend({**r, "added_at": now} for r in ranked)

    if watchlist:  # keep yesterday's list if every screen failed
        WATCHLIST_FILE.write_text(json.dumps(watchlist, indent=2))
    return {"added": watchlist, "total_watchlist": len(watchlist)}


if __name__ == "__main__":
    r = run()
    print(f"Watchlist rebuilt: {r['total_watchlist']} candidate(s).")
    for c in r["added"]:
        print(f"  [{c['bucket']}] {c['ticker']} score={c['score']:.1f} conv={c['conviction']} @ ${c['last_price']:.2f}")
        print(f"      {c['thesis']}")
