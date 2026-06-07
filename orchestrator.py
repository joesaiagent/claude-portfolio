"""Orchestrator: runs each agent in order, prints a consolidated report."""
import argparse
import traceback

from agents.tracker import agent as tracker
from agents.research import agent as research
from agents.allocator import agent as allocator
from agents.content import agent as content
from agents.analytics import agent as analytics


def run_safe(name: str, fn) -> dict | None:
    print(f"\n--- {name} ---")
    try:
        return fn()
    except Exception as e:
        print(f"[{name}] FAILED: {e}")
        traceback.print_exc()
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip", nargs="*", default=[], help="agents to skip (research, allocator, tracker, content, analytics)")
    args = parser.parse_args()

    results = {}

    if "tracker" not in args.skip:
        results["tracker"] = run_safe("TRACKER", tracker.run)
    if "research" not in args.skip:
        results["research"] = run_safe("RESEARCH", research.run)
    if "allocator" not in args.skip:
        results["allocator"] = run_safe("ALLOCATOR", allocator.run)
    if "content" not in args.skip:
        results["content"] = run_safe("CONTENT", content.run)
    if "analytics" not in args.skip:
        results["analytics"] = run_safe("ANALYTICS", analytics.run)

    print("\n========== CONSOLIDATED REPORT ==========")

    if results.get("tracker"):
        print("\n[Portfolio]")
        print(results["tracker"].get("summary", "(no summary)"))

    if results.get("research"):
        added = results["research"].get("added", [])
        print(f"\n[Research] {len(added)} new candidate(s) added to watchlist.")
        for c in added:
            print(f"  - {c.get('ticker')}: {c.get('thesis', '')[:80]}")

    if results.get("allocator"):
        suggs = results["allocator"].get("suggestions", [])
        print(f"\n[Allocator] {len(suggs)} buy suggestion(s) pending manual approval.")
        for s in suggs:
            print(f"  - {s.get('ticker')} x{s.get('shares')} @ <={s.get('max_price')}")

    if results.get("content"):
        drafts = results["content"].get("drafts", [])
        print(f"\n[Content] {len(drafts)} draft post(s) queued.")

    if results.get("analytics"):
        print("\n[Analytics]")
        print(results["analytics"].get("summary", "(no summary)"))

    print("\n=========================================")


if __name__ == "__main__":
    main()
