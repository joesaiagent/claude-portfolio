"""Analytics: free-tier. Pure Python KPI counting. No LLM, no APIs (X analytics stubbed)."""
import json
from collections import Counter

from dotenv import load_dotenv

from agents._state import POSTS_FILE

load_dotenv()


def run() -> dict:
    posts = json.loads(POSTS_FILE.read_text()) if POSTS_FILE.exists() else []
    posted = [p for p in posts if p.get("status") == "posted"]
    drafts = [p for p in posts if p.get("status") == "draft"]
    by_topic = Counter(p.get("topic", "?") for p in posted)
    by_platform = Counter(p.get("platform", "?") for p in posted)

    summary_lines = [
        f"Posted total: {len(posted)}",
        f"Drafts pending: {len(drafts)}",
    ]
    if by_topic:
        summary_lines.append(f"By topic: {dict(by_topic)}")
    if by_platform:
        summary_lines.append(f"By platform: {dict(by_platform)}")

    # TODO: when X API keys are present, fetch impressions/likes/RTs per post here.
    return {
        "summary": "\n".join(summary_lines),
        "posted_count": len(posted),
        "draft_count": len(drafts),
        "by_topic": dict(by_topic),
    }


if __name__ == "__main__":
    r = run()
    print("=== ANALYTICS ===")
    print(r["summary"])
