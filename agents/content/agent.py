"""Content: free-tier hybrid.
- 2-3 templated factual posts (P/L, position open, lottery outcome) — $0 cost
- 1 Haiku 'narrative' post per cycle for the human voice — ~$0.005/call
Auto-includes affiliate links from .env.
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
    WATCHLIST_FILE,
    is_autonomous,
    load_state,
)

load_dotenv()

MODEL = "claude-haiku-4-5"


def _affiliate_footer() -> str:
    """Optional affiliate link block — set in .env. Empty if not configured."""
    parts = []
    if os.getenv("ALPACA_REFERRAL_URL"):
        parts.append(f"Trading via Alpaca: {os.environ['ALPACA_REFERRAL_URL']}")
    if os.getenv("RH_REFERRAL_URL"):
        parts.append(f"Robinhood: {os.environ['RH_REFERRAL_URL']}")
    return ("\n\n" + " | ".join(parts)) if parts else ""


def _truncate_with_footer(text: str, footer: str, limit: int = 280) -> str:
    if not footer:
        return text[:limit]
    body_limit = limit - len(footer)
    return (text[:body_limit] + footer) if body_limit > 0 else text[:limit]


def template_posts(state: dict, tracker: dict) -> list[dict]:
    """Generate fact-based posts from state + tracker output. No LLM cost."""
    posts = []
    pl = tracker.get("total_pl_dollars", 0)
    pct = tracker.get("total_pl_pct", 0)
    value = tracker.get("total_portfolio_value", state["starting_capital"])
    arrow = "📈" if pl >= 0 else "📉"

    if tracker.get("positions"):
        posts.append({
            "topic": "performance",
            "text": (
                f"{arrow} Day update: ${value:.2f} ({pct:+.2f}%, P/L ${pl:+.2f})\n"
                f"Core: ${tracker['buckets']['core']['current_value']:.2f}  "
                f"Swing: ${tracker['buckets']['swing']['current_value']:.2f}  "
                f"Lottery: ${tracker['buckets']['lottery']['current_value']:.2f}"
            ),
        })
    else:
        posts.append({
            "topic": "update",
            "text": "Cash on the sidelines, screener running. Next entries when premarket fires.",
        })

    # Notable mover
    movers = sorted(tracker.get("positions", []), key=lambda p: abs(p.get("pl_pct", 0)), reverse=True)
    if movers:
        m = movers[0]
        verb = "ripping" if m["pl_pct"] > 0 else "underwater"
        posts.append({
            "topic": "thesis",
            "text": (
                f"{m['ticker']} {verb} {m['pl_pct']:+.2f}% from entry. "
                f"Bucket: {m.get('bucket','?')}. Holding."
            ),
        })

    return posts


def narrative_post(state: dict, tracker: dict) -> dict | None:
    """ONE Haiku call per cycle for the human-voice post. ~$0.005/call."""
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=(
                "You write social posts for an autonomous AI account trying to turn $300 into $10K. "
                "ONE post, ≤220 chars, conversational, human, no hashtags, no emojis except maybe one. "
                "Output the post text directly — no quotes, no preamble, no JSON."
            ),
            messages=[{
                "role": "user",
                "content": json.dumps({
                    "starting": state["starting_capital"],
                    "current_value": tracker.get("total_portfolio_value"),
                    "pl_pct": tracker.get("total_pl_pct"),
                    "buckets": tracker.get("buckets", {}),
                    "top_positions": tracker.get("positions", [])[:3],
                }),
            }],
        )
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        return {"topic": "narrative", "text": text} if text else None
    except Exception as e:
        print(f"[content] narrative skipped: {e}")
        return None


def run() -> dict:
    state = load_state()
    tracker = json.loads(TRACKER_REPORT.read_text()) if TRACKER_REPORT.exists() else {}

    drafts_raw = template_posts(state, tracker)
    n = narrative_post(state, tracker)
    if n:
        drafts_raw.append(n)

    footer = _affiliate_footer()
    now = datetime.now(timezone.utc).isoformat()
    drafts = []
    for d in drafts_raw:
        text = _truncate_with_footer(d["text"], footer)
        post = {
            "id": str(uuid.uuid4()),
            "text": text,
            "topic": d["topic"],
            "platform": "x",
            "status": "draft",
            "created_at": now,
        }
        if is_autonomous(state):
            result = social.post_everywhere(post["text"])
            post["fanout"] = result["results"]
            if result.get("posted"):
                post["status"] = "posted"
                post["posted_at"] = now
                post["posted_platforms"] = [r["platform"] for r in result["results"] if r["posted"]]
            else:
                post["post_error"] = "no platform credentials configured"
        drafts.append(post)

    existing = json.loads(POSTS_FILE.read_text()) if POSTS_FILE.exists() else []
    POSTS_FILE.write_text(json.dumps(existing + drafts, indent=2))

    return {
        "drafts": drafts,
        "autonomous": is_autonomous(state),
        "posted_count": sum(1 for d in drafts if d["status"] == "posted"),
    }


if __name__ == "__main__":
    r = run()
    print(f"Created {len(r['drafts'])} post(s). Auto-posted: {r['posted_count']}.")
    for d in r["drafts"]:
        marker = "✓" if d["status"] == "posted" else ("✗" if d.get("post_error") else "→")
        print(f"  {marker} [{d['topic']}] {d['text'][:120]}...")
