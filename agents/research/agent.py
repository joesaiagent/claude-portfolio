"""Research: free-tier multi-source. yfinance momentum screen, then enrich the top
picks with Alpha Vantage news sentiment + analyst consensus. No LLM cost."""
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

from agents._analyst import enrich_with_analyst
from agents._intel import EARNINGS_ENTRY_BLOCK_DAYS, earnings_within, enrich
from agents._screener import screen_bucket
from agents._state import WATCHLIST_FILE, atomic_write_text

load_dotenv()

# Entry-extension filter: momentum selects the names, but CHASING an already
# -extended name puts the entry a stone's throw from its stop (the MU/SMH/AMD
# stop-outs — bought stretched, mean-reverted straight through -15%). A name
# too far above its 50DMA or with RSI this hot gets skipped; if it's a real
# trend it will still rank tomorrow at a saner distance. Lottery gets a wide
# band — extension is partly the point there.
EXT_MAX_ABOVE_MA50 = {"core": 12.0, "swing": 18.0, "lottery": 30.0}
EXT_MAX_RSI = 75.0


def _drop_extended(ranked: list[dict]) -> list[dict]:
    """Drop candidates too extended to enter (above_ma50 / rsi14 from the
    screener). Missing fields pass through — only positively-confirmed
    extension blocks, mirroring the earnings filter's fail-open stance."""
    out = []
    for c in ranked:
        above = c.get("above_ma50")
        rsi = c.get("rsi14")
        cap = EXT_MAX_ABOVE_MA50.get(c["bucket"], 999.0)
        if above is not None and above > cap:
            print(f"[research] {c['ticker']}: {above:+.1f}% above 50DMA > {cap:.0f}% cap — too extended, skipping")
            continue
        if rsi is not None and rsi > EXT_MAX_RSI:
            print(f"[research] {c['ticker']}: RSI {rsi:.0f} > {EXT_MAX_RSI:.0f} — overbought, skipping")
            continue
        out.append(c)
    return out


def _drop_pre_earnings_swing(ranked: list[dict]) -> list[dict]:
    """Drop SWING candidates with a confirmed earnings report inside the hold
    window — the bucket plays earnings REACTIONS (post-print momentum), it does
    not gamble the print against an -8% stop. Unknown (None) passes through:
    only a positively-confirmed date blocks an entry."""
    out = []
    for c in ranked:
        if c["bucket"] == "swing" and earnings_within(c["ticker"], EARNINGS_ENTRY_BLOCK_DAYS):
            print(f"[research] {c['ticker']}: earnings within {EARNINGS_ENTRY_BLOCK_DAYS}d — skipping swing entry")
            continue
        out.append(c)
    return out


def run(buckets: tuple[str, ...] = ("core", "swing")) -> dict:
    """Rebuild the watchlist fresh every run. (Previously skipped re-screening once
    the list hit 15 entries, so the allocator bought off multi-day-old scores.)

    Lottery retired 2026-09-01: -$15.16 realized, zero wins, and the $30 cap
    froze the sleeve for weeks at a time. Its capital moved to core (see
    allocation_targets), where the allocator now pyramids proven winners
    instead of buying fresh long-shots. Exit rules for any residual lottery
    position remain in exits.py; pass buckets explicitly to screen it again."""
    watchlist = []
    now = datetime.now(timezone.utc).isoformat()
    for bucket in buckets:
        try:
            ranked = screen_bucket(bucket, top_n=5)
        except Exception as e:
            print(f"[research] {bucket} screening failed: {e}")
            continue
        ranked = _drop_extended(ranked)  # before enrich: no API spend on rejected names
        ranked = enrich(ranked)  # news sentiment + analyst consensus, re-ranks
        ranked = [r for r in ranked if not r.get("excluded")]  # drop excluded sectors (healthcare)
        ranked = _drop_pre_earnings_swing(ranked)
        watchlist.extend({**r, "added_at": now} for r in ranked)

    # ONE batched Claude (Haiku) call over the whole watchlist — headline
    # judgment the quant factors can't see, applied as a bounded score tilt.
    # Runs after all buckets so it's a single ~$0.005 call, not three, and
    # degrades to a no-op on any failure.
    watchlist = enrich_with_analyst(watchlist)

    if watchlist:  # keep yesterday's list if every screen failed
        atomic_write_text(WATCHLIST_FILE, json.dumps(watchlist, indent=2))
    return {"added": watchlist, "total_watchlist": len(watchlist)}


if __name__ == "__main__":
    r = run()
    print(f"Watchlist rebuilt: {r['total_watchlist']} candidate(s).")
    for c in r["added"]:
        print(f"  [{c['bucket']}] {c['ticker']} score={c['score']:.1f} conv={c['conviction']} @ ${c['last_price']:.2f}")
        print(f"      {c['thesis']}")
