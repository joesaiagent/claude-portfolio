"""X (Twitter) posting wrapper. No-op gracefully if creds missing."""
import os

from dotenv import load_dotenv

load_dotenv()


def _have_x_creds() -> bool:
    return all(os.getenv(k) for k in (
        "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
    ))


def post_tweet(text: str) -> dict:
    if not _have_x_creds():
        return {"posted": False, "reason": "X credentials not set in .env"}
    import tweepy
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text[:280])
    return {"posted": True, "tweet_id": resp.data["id"], "text": text[:280]}
