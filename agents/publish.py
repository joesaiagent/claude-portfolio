"""Publish current portfolio state + posts as static JSON in /docs for GitHub Pages.
Then git commit + push so the public site updates.
"""
import json
import os
import subprocess
from pathlib import Path

from agents import notify
from agents._state import POSTS_FILE, TRACKER_REPORT, simulating

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _atomic_write_json(text: str, dest: Path) -> None:
    """Atomic write mirroring _state.save_state: validate the payload parses as
    JSON, write to a temp file in the SAME directory (docs/), then os.replace()
    (atomic on POSIX, same filesystem). A process kill mid-write can then never
    commit a truncated/corrupt JSON file to the public GitHub Pages site."""
    json.loads(text)  # round-trip validate; raises if the payload is not valid JSON
    tmp = dest.parent / (dest.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, dest)  # atomic rename


def run(push: bool = True) -> dict:
    if simulating():
        return {"pushed": False, "reason": "simulate — no publish"}
    DOCS.mkdir(exist_ok=True)
    status_dest = DOCS / "status.json"
    posts_dest = DOCS / "posts.json"

    try:
        if TRACKER_REPORT.exists():
            # Read the source and atomically replace the destination, so the
            # published status.json is never left partially written.
            _atomic_write_json(TRACKER_REPORT.read_text(), status_dest)
        else:
            _atomic_write_json(json.dumps({"total_portfolio_value": 300.0}), status_dest)

        if POSTS_FILE.exists():
            # Publish all posts EXCEPT ones marked deleted (e.g. a tweet removed
            # from X) — they stay in the source log for the record but must not
            # resurface on the public site on the next publish.
            posts = json.loads(POSTS_FILE.read_text())
            visible = [p for p in posts if p.get("status") != "deleted"]
            _atomic_write_json(json.dumps(visible, indent=2), posts_dest)
        else:
            _atomic_write_json("[]", posts_dest)
    except (json.JSONDecodeError, ValueError) as e:
        # A source file was itself corrupt/truncated; skip publishing garbage.
        notify.send("publish: invalid JSON, skipping publish", str(e)[:300], level="warning")
        return {"pushed": False, "reason": f"invalid JSON: {e}"}

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
