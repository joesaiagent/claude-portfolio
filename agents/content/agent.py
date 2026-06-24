"""Content: ONE high-quality X post per day. No URLs in tweets (X charges $0.20/URL).

Cost per day:
- 1 Haiku narrative call: ~$0.005
- 1 X post (plain text): $0.015 (Pay Per Use tier)
- Daily total: ~$0.02/day = ~$0.60/month
"""
import json
import os
import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv

ET = ZoneInfo("America/New_York")

from agents import broker, social
from agents._state import (
    POSTS_FILE,
    TRACKER_REPORT,
    is_autonomous,
    load_state,
)

load_dotenv()

MODEL = "claude-haiku-4-5"

# "Day 1" of the public challenge = 2026-06-16, fixed by the established post
# history (the 6/17 post said "Day 2", 6/18 "Day 3", 6/22 "Day 4"; counting
# trading days back from there lands Day 1 on Tue 6/16). NOT the 6/12 real-money
# go-live — anchoring there over-counted by 2. The "Day N" counter = trading
# days since this date (market calendar), so it steps by exactly 1 each posting
# day: 6/22=4, 6/23=5, 6/24=6, 6/25=7, ... no skips/repeats. Previously the LLM
# invented the number (five different days all "Day 1", then a 4->7 jump).
LAUNCH_DATE = date(2026, 6, 16)


def day_number() -> int:
    return max(1, broker.trading_days_since(LAUNCH_DATE))


def _clip(text: str, limit: int = 278) -> str:
    """Trim to X's length budget without an ugly ending. Prefer cutting at the
    last COMPLETE sentence; else fall back to a word boundary. Avoids the
    dangling 'Portfolio up 2.41% to' mid-sentence cuts."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Last sentence terminator (".", "!", "?") followed by a space — a real
    # sentence end, not the "." in "$140.25" or "2.41%".
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit * 0.5:
        return cut[:end + 1].strip()
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-—…")


def template_fallback(state: dict, tracker: dict, day: int) -> str:
    """Bare-bones factual post if the LLM call fails. No URLs."""
    pl = tracker.get("total_pl_dollars", 0)
    pct = tracker.get("total_pl_pct", 0)
    value = tracker.get("total_portfolio_value", state["starting_capital"])
    arrow = "📈" if pl >= 0 else "📉"
    if not tracker.get("positions"):
        return f"{arrow} Day {day}: cash on the sidelines. ${value:.2f} ready. Next entries at premarket."
    return (
        f"{arrow} Day {day} close: ${value:.2f} ({pct:+.2f}%, P/L ${pl:+.2f}). "
        f"Core ${tracker['buckets']['core']['current_value']:.2f} · "
        f"Swing ${tracker['buckets']['swing']['current_value']:.2f} · "
        f"Lottery ${tracker['buckets']['lottery']['current_value']:.2f}"
    )


def narrative_post(state: dict, tracker: dict, day: int) -> str | None:
    """ONE Haiku call. Generates the day's single human-voice post.
    NO URLs in output — X charges $0.20 per URL-containing tweet vs $0.015 plain.
    """
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=(
                "You write a single daily social post for an autonomous AI account trying to "
                "turn $300 into $10K via a 3-bucket strategy (core/swing/lottery). "
                "ONE post, ≤260 chars (leave room for safety). "
                "Conversational, honest, no hashtags, no preamble, no quotes. "
                "The day number is provided in the input as 'day' — if you reference the day, "
                "use that EXACT number. Never invent or guess a different day number. "
                "CRITICAL: do NOT include URLs, links, or 't.co' anywhere — X charges 13x more "
                "for URL-containing tweets and we can't afford it. Mention the @claudeinvesting "
                "handle is allowed but no http/https links. "
                "Output the post text directly."
            ),
            messages=[{
                "role": "user",
                "content": json.dumps({
                    "day": day,
                    "starting": state["starting_capital"],
                    "current_value": tracker.get("total_portfolio_value"),
                    "pl_pct": tracker.get("total_pl_pct"),
                    "pl_dollars": tracker.get("total_pl_dollars"),
                    "buckets": tracker.get("buckets", {}),
                    "top_positions": tracker.get("positions", [])[:3],
                }),
            }],
        )
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        # Strip URLs defensively — even if model ignored instructions.
        if "http://" in text or "https://" in text or "t.co/" in text:
            import re
            text = re.sub(r"https?://\S+|t\.co/\S+", "", text).strip()
        return _clip(text) if text else None
    except Exception as e:
        print(f"[content] narrative call failed: {e}")
        return None


def todays_trades(state: dict) -> list[dict]:
    """Order-log entries from today's ET TRADING DAY — the buys/sells the midday
    post talks about, each carrying the rationale/factors recorded at placement.
    Grouped by ET (not UTC) so evening-ET trades don't roll into the next day."""
    today = datetime.now(ET).date()
    out = []
    for o in state.get("order_log", []):
        ts = o.get("ts")
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts).astimezone(ET).date() == today:
                out.append(o)
        except (ValueError, TypeError):
            continue
    return out


def midday_narrative(state: dict, tracker: dict, day: int, trades: list[dict]) -> str | None:
    """ONE Haiku call for the midday update: what we traded today, how much cash
    got deployed, and WHY (the factors behind the moves). Same voice as the daily
    post, no URLs. Returns None on failure so the caller can fall back."""
    buys = [t for t in trades if t.get("side") == "buy"]
    sells = [t for t in trades if t.get("side") == "sell"]
    deployed = round(sum(t.get("estimated_cost", 0) or 0 for t in buys), 2)
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=MODEL,
            max_tokens=220,
            system=(
                "You write the MIDDAY update post for an autonomous AI account turning $300 "
                "into $10K via a 3-bucket strategy (core/swing/lottery). This is a separate, "
                "shorter post from the end-of-day recap: focus on what was TRADED today, how "
                "much cash was deployed, and the REASONING/factors behind the moves "
                "(momentum, news sentiment, analyst/insider signals, etc.). If there were no "
                "new trades, give a brief 'holding, here's why' note instead. "
                "ONE post, <=260 chars. Conversational, honest, no hashtags, no preamble, no "
                "quotes. The day number is in the input as 'day' — use that EXACT number, "
                "never invent one. CRITICAL: no URLs/links/'t.co' anywhere (X charges 13x more). "
                "The @claudeinvesting handle is fine but no http/https links. Output the post text directly."
            ),
            messages=[{
                "role": "user",
                "content": json.dumps({
                    "day": day,
                    "portfolio_value": tracker.get("total_portfolio_value"),
                    "pl_pct": tracker.get("total_pl_pct"),
                    "cash_deployed_today": deployed,
                    "buys": [{"ticker": t.get("ticker"), "bucket": t.get("bucket"),
                              "cost": t.get("estimated_cost"), "why": t.get("rationale", "")} for t in buys],
                    "sells": [{"ticker": t.get("ticker"), "bucket": t.get("bucket"),
                               "why": t.get("reason", ""), "pl": t.get("realized_pl_dollars")} for t in sells],
                    "buckets": tracker.get("buckets", {}),
                }),
            }],
        )
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        if "http://" in text or "https://" in text or "t.co/" in text:
            import re
            text = re.sub(r"https?://\S+|t\.co/\S+", "", text).strip()
        return _clip(text) if text else None
    except Exception as e:
        print(f"[content] midday call failed: {e}")
        return None


def midday_fallback(state: dict, tracker: dict, day: int, trades: list[dict]) -> str:
    """Factual midday post if the LLM call fails. No URLs."""
    buys = [t for t in trades if t.get("side") == "buy"]
    sells = [t for t in trades if t.get("side") == "sell"]
    pct = tracker.get("total_pl_pct", 0)
    if not buys and not sells:
        return f"Day {day} midday: no new entries — holding the book, watching the screener. ({pct:+.2f}%)"
    deployed = round(sum(t.get("estimated_cost", 0) or 0 for t in buys), 2)
    names = ", ".join(f"{t['ticker']}" for t in buys[:3]) or "—"
    parts = []
    if buys:
        parts.append(f"deployed ${deployed:.0f} into {names}")
    if sells:
        parts.append(f"trimmed {', '.join(t['ticker'] for t in sells[:2])}")
    return f"Day {day} midday: {'; '.join(parts)}. Book {pct:+.2f}%."


def _emit(state: dict, text: str, topic: str) -> dict:
    """Post `text` to every configured platform and append it to the post log.
    Shared by the daily recap and the midday update."""
    now = datetime.now(timezone.utc).isoformat()
    post = {
        "id": str(uuid.uuid4()),
        "text": text,
        "topic": topic,
        "platform": "x",
        "status": "draft",
        "created_at": now,
    }
    if is_autonomous(state):
        result = social.post_everywhere(text)
        post["fanout"] = result["results"]
        if result.get("posted"):
            post["status"] = "posted"
            post["posted_at"] = now
            post["posted_platforms"] = [r["platform"] for r in result["results"] if r["posted"]]
        else:
            post["post_error"] = "no platform credentials configured"

    existing = json.loads(POSTS_FILE.read_text()) if POSTS_FILE.exists() else []
    POSTS_FILE.write_text(json.dumps(existing + [post], indent=2))
    return {"post": post, "autonomous": is_autonomous(state), "posted": post["status"] == "posted"}


def run() -> dict:
    """Post-close: the one daily recap post."""
    state = load_state()
    tracker = json.loads(TRACKER_REPORT.read_text()) if TRACKER_REPORT.exists() else {}
    day = day_number()
    text = narrative_post(state, tracker, day) or template_fallback(state, tracker, day)
    return _emit(state, text, "daily_summary")


def run_midday() -> dict:
    """Midday: an update on today's trades, cash deployed, and the reasoning."""
    state = load_state()
    tracker = json.loads(TRACKER_REPORT.read_text()) if TRACKER_REPORT.exists() else {}
    day = day_number()
    trades = todays_trades(state)
    text = midday_narrative(state, tracker, day, trades) or midday_fallback(state, tracker, day, trades)
    return _emit(state, text, "midday_update")


if __name__ == "__main__":
    r = run()
    marker = "✓ posted" if r["posted"] else ("→ draft" if not r["autonomous"] else "✗ failed")
    print(f"{marker}: {r['post']['text']}")
    if r["post"].get("post_error"):
        print(f"  error: {r['post']['post_error']}")
