"""Publish current portfolio state + posts as static JSON in /docs for GitHub Pages.
Then git commit + push so the public site updates.
"""
import json
import shutil
import subprocess
from pathlib import Path

from agents._state import POSTS_FILE, TRACKER_REPORT, simulating

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def run(push: bool = True) -> dict:
    if simulating():
        return {"pushed": False, "reason": "simulate — no publish"}
    DOCS.mkdir(exist_ok=True)
    status_dest = DOCS / "status.json"
    posts_dest = DOCS / "posts.json"

    if TRACKER_REPORT.exists():
        shutil.copy(TRACKER_REPORT, status_dest)
    else:
        status_dest.write_text(json.dumps({"total_portfolio_value": 300.0}))

    if POSTS_FILE.exists():
        shutil.copy(POSTS_FILE, posts_dest)
    else:
        posts_dest.write_text("[]")

    if not push:
        return {"pushed": False, "reason": "push=False"}

    try:
        # Only commit + push if there are actual changes
        status = subprocess.run(
            ["git", "status", "--porcelain", "docs/"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if not status.stdout.strip():
            return {"pushed": False, "reason": "no changes"}
        subprocess.run(["git", "add", "docs/"], cwd=ROOT, check=True)
        subprocess.run(
            ["git", "commit", "-m", "publish: update portfolio status"],
            cwd=ROOT, check=True, capture_output=True,
        )
        subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, check=True, capture_output=True)
        return {"pushed": True}
    except subprocess.CalledProcessError as e:
        return {"pushed": False, "reason": f"git error: {e.stderr.decode() if e.stderr else e}"}
    except Exception as e:
        return {"pushed": False, "reason": str(e)}


if __name__ == "__main__":
    r = run()
    print(r)
