"""Publish current portfolio state + posts as static JSON in /docs for GitHub Pages.
Then git commit + push so the public site updates.
"""
import json
import shutil
import subprocess
from pathlib import Path

from agents import notify
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
        # Publish all posts EXCEPT ones marked deleted (e.g. a tweet removed from
        # X) — they stay in the source log for the record but must not resurface
        # on the public site on the next publish.
        posts = json.loads(POSTS_FILE.read_text())
        visible = [p for p in posts if p.get("status") != "deleted"]
        posts_dest.write_text(json.dumps(visible, indent=2))
    else:
        posts_dest.write_text("[]")

    if not push:
        return {"pushed": False, "reason": "push=False"}

    def _git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)

    try:
        # Only commit + push if there are actual changes
        status = _git("status", "--porcelain", "docs/")
        if not status.stdout.strip():
            return {"pushed": False, "reason": "no changes"}
        subprocess.run(["git", "add", "docs/"], cwd=ROOT, check=True)
        subprocess.run(
            ["git", "commit", "-m", "publish: update portfolio status"],
            cwd=ROOT, check=True, capture_output=True,
        )

        pushed = _git("push", "origin", "main")
        if pushed.returncode != 0:
            # Remote moved ahead (non-fast-forward) or a transient error. Rebase
            # our docs commit on top of origin and retry ONCE. --autostash sets
            # aside the always-dirty data/portfolio_state.json for the rebase.
            # Without this, the local commit can never push and every later cycle
            # silently re-commits onto a branch that will never reach the site.
            rebase = _git("pull", "--rebase", "--autostash", "origin", "main")
            if rebase.returncode != 0:
                _git("rebase", "--abort")  # never leave the repo mid-rebase
                reason = (rebase.stderr or rebase.stdout or "rebase failed").strip()[:300]
                notify.send(
                    "publish: git rebase failed",
                    f"Could not reconcile with origin/main; public site is NOT updating.\n{reason}",
                    level="critical",
                )
                return {"pushed": False, "reason": f"git rebase failed: {reason}"}
            pushed = _git("push", "origin", "main")

        if pushed.returncode != 0:
            reason = (pushed.stderr or pushed.stdout or "unknown").strip()[:300]
            notify.send(
                "publish: git push failed",
                f"Public site is NOT updating after rebase+retry.\n{reason}",
                level="critical",
            )
            return {"pushed": False, "reason": f"git push failed: {reason}"}
        return {"pushed": True}
    except subprocess.CalledProcessError as e:
        reason = e.stderr.decode() if isinstance(e.stderr, (bytes, bytearray)) and e.stderr else str(e)
        notify.send("publish: git commit failed", str(reason)[:300], level="critical")
        return {"pushed": False, "reason": f"git error: {reason}"}
    except Exception as e:
        notify.send("publish: unexpected error", str(e)[:300], level="warning")
        return {"pushed": False, "reason": str(e)}


if __name__ == "__main__":
    r = run()
    print(r)
