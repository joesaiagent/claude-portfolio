"""Multi-platform social posting. Fans posts out to all configured platforms.

Supported (all official, free APIs):
- X (Twitter) — requires X_API_KEY etc.
- Bluesky — requires BLUESKY_HANDLE + BLUESKY_APP_PASSWORD
- Mastodon — requires MASTODON_INSTANCE + MASTODON_ACCESS_TOKEN

If a platform's creds are missing, that platform is silently skipped (graceful no-op).
"""
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()


# ---------- X / Twitter ----------

def _have_x() -> bool:
    return all(os.getenv(k) for k in (
        "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
    ))


def post_x(text: str) -> dict:
    if not _have_x():
        return {"platform": "x", "posted": False, "reason": "creds missing"}
    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=os.environ["X_API_KEY"],
            consumer_secret=os.environ["X_API_SECRET"],
            access_token=os.environ["X_ACCESS_TOKEN"],
            access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        )
        resp = client.create_tweet(text=text[:280])
        return {"platform": "x", "posted": True, "id": resp.data["id"]}
    except Exception as e:
        return {"platform": "x", "posted": False, "reason": str(e)[:200]}


# ---------- Bluesky ----------

def _have_bluesky() -> bool:
    return bool(os.getenv("BLUESKY_HANDLE") and os.getenv("BLUESKY_APP_PASSWORD"))


def post_bluesky(text: str) -> dict:
    if not _have_bluesky():
        return {"platform": "bluesky", "posted": False, "reason": "creds missing"}
    try:
        import requests
        # 1. Create session
        s = requests.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier": os.environ["BLUESKY_HANDLE"],
                  "password": os.environ["BLUESKY_APP_PASSWORD"]},
            timeout=15,
        ).json()
        jwt = s["accessJwt"]
        did = s["did"]
        # 2. Create post
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        r = requests.post(
            "https://bsky.social/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "repo": did,
                "collection": "app.bsky.feed.post",
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": text[:300],
                    "createdAt": now,
                },
            },
            timeout=15,
        ).json()
        return {"platform": "bluesky", "posted": True, "id": r.get("uri", "")}
    except Exception as e:
        return {"platform": "bluesky", "posted": False, "reason": str(e)[:200]}


# ---------- Mastodon ----------

def _have_mastodon() -> bool:
    return bool(os.getenv("MASTODON_INSTANCE") and os.getenv("MASTODON_ACCESS_TOKEN"))


def post_mastodon(text: str) -> dict:
    if not _have_mastodon():
        return {"platform": "mastodon", "posted": False, "reason": "creds missing"}
    try:
        import requests
        instance = os.environ["MASTODON_INSTANCE"].rstrip("/")
        r = requests.post(
            f"{instance}/api/v1/statuses",
            headers={"Authorization": f"Bearer {os.environ['MASTODON_ACCESS_TOKEN']}"},
            data={"status": text[:500]},
            timeout=15,
        ).json()
        return {"platform": "mastodon", "posted": True, "id": str(r.get("id", ""))}
    except Exception as e:
        return {"platform": "mastodon", "posted": False, "reason": str(e)[:200]}


# ---------- Unified fan-out ----------

def post_tweet(text: str) -> dict:
    """Legacy single-platform interface (X only). Kept for back-compat."""
    return post_x(text)


def post_everywhere(text: str) -> dict:
    """Fan one post out to every configured platform. Returns per-platform results."""
    results = [post_x(text), post_bluesky(text), post_mastodon(text)]
    posted_count = sum(1 for r in results if r["posted"])
    return {
        "posted_count": posted_count,
        "results": results,
        "posted": posted_count > 0,
        "tweet_id": next((r["id"] for r in results if r["posted"] and r["platform"] == "x"), None),
    }
