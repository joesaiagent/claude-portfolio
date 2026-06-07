"""Research: free-tier. Pulls yfinance data + Python scoring rules. No LLM cost."""
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents._screener import screen_bucket
from agents._state import WATCHLIST_FILE

load_dotenv()


def run(buckets: tuple[str, ...] = ("core", "swing", "lottery")) -> dict:
    existing = json.loads(WATCHLIST_FILE.read_text()) if WATCHLIST_FILE.exists() else []

    # Skip if we already have plenty. Re-screening daily anyway via runner.
    if len(existing) >= 15:
        return {"added": [], "total_watchlist": len(existing), "skipped": "watchlist >= 15"}

    existing_keys = {(c.get("ticker"), c.get("bucket")) for c in existing}

    added = []
    for bucket in buckets:
        try:
            ranked = screen_bucket(bucket, top_n=5)
        except Exception as e:
            print(f"[research] {bucket} screening failed: {e}")
            continue
        now = datetime.now(timezone.utc).isoformat()
        for r in ranked:
            key = (r["ticker"], r["bucket"])
            if key in existing_keys:
                continue
            added.append({**r, "added_at": now})
            existing_keys.add(key)

    final = existing + added
    WATCHLIST_FILE.write_text(json.dumps(final, indent=2))
    return {"added": added, "total_watchlist": len(final)}


if __name__ == "__main__":
    r = run()
    print(f"Added {len(r['added'])} candidate(s). Watchlist size: {r['total_watchlist']}.")
    for c in r["added"]:
        print(f"  [{c['bucket']}] {c['ticker']} score={c['score']:.1f} conv={c['conviction']} @ ${c['last_price']:.2f}")
