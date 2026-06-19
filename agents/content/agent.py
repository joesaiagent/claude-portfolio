"""Content: ONE high-quality X post per day. No URLs in tweets (X charges $0.20/URL).

Cost per day:
- 1 Haiku narrative call: ~$0.005
- 1 X post (plain text): $0.015 (Pay Per Use tier)
- Daily total: ~$0.02/day = ~$0.60/month
"""
import json
import os
import uuid
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

from agents import social
from agents._state import (
    POSTS_FILE,
    TRACKER_REPORT,
    is_autonomous,
    load_state,
)

load_dotenv()

MODEL = "claude-haiku-4-5"


def template_fallback(state: dict, tracker: dict) -> str:
    """Bare-bones factual post if the LLM call fails. No URLs."""
    pl = tracker.get("total_pl_dollars", 0)
    pct = tracker.get("total_pl_pct", 0)
    value = tracker.get("total_portfolio_value", state["starting_capital"])
    arrow = "📈" if pl >= 0 else "📉"
    if not tracker.get("positions"):
        return f"{arrow} Cash on the sidelines. ${value:.2f} ready. Next entries at premarket."
    return (
        f"{arrow} Day close: ${value:.2f} ({pct:+.2f}%, P/L ${pl:+.2f}). "
        f"Core ${tracker['buckets']['core']['current_value']:.2f} · "
        f"Swing ${tracker['buckets']['swing']['current_value']:.2f} · "
        f"Lottery ${tracker['buckets']['lottery']['current_value']:.2f}"
    )


def narrative_post(state: dict, tracker: dict) -> str | None:
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
                "CRITICAL: do NOT include URLs, links, or 't.co' anywhere — X charges 13x more "
                "for URL-containing tweets and we can't afford it. Mention the @claudeinvesting "
                "handle is allowed but no http/https links. "
                "Output the post text directly."
            ),
            messages=[{
                "role": "user",
                "content": json.dumps({
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
        return text[:270] if text else None
    except Exception as e:
        print(f"[content] narrative call failed: {e}")
        return None


def run() -> dict:
    state = load_state()
    tracker = json.loads(TRACKER_REPORT.read_text()) if TRACKER_REPORT.exists() else {}

    text = narrative_post(state, tracker) or template_fallback(state, tracker)

    now = datetime.now(timezone.utc).isoformat()
    post = {
        "id": str(uuid.uuid4()),
        "text": text,
        "topic": "daily_summary",
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

    return {
        "post": post,
        "autonomous": is_autonomous(state),
        "posted": post["status"] == "posted",
    }


if __name__ == "__main__":
    r = run()
    marker = "✓ posted" if r["posted"] else ("→ draft" if not r["autonomous"] else "✗ failed")
    print(f"{marker}: {r['post']['text']}")
    if r["post"].get("post_error"):
        print(f"  error: {r['post']['post_error']}")
