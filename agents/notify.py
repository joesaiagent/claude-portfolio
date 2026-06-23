"""Outbound alerting for the health watchdog. Push / email, all optional and
env-gated. Tries every configured channel; a channel with no creds is a silent
no-op (never raises). Returns the list of channels that actually delivered.

Channels (configure any subset in .env):
  - ntfy   : NTFY_TOPIC   (+ optional NTFY_SERVER, default https://ntfy.sh)
             Zero-setup push: pick a hard-to-guess topic, install the ntfy app
             (iOS/Android) or open https://ntfy.sh/<topic>, and subscribe.
  - pushover: PUSHOVER_TOKEN + PUSHOVER_USER
  - email   : ALERT_EMAIL_TO + SMTP_HOST + SMTP_USER + SMTP_PASS (+ SMTP_PORT)
"""
import os
import smtplib
from email.message import EmailMessage

import requests
from dotenv import load_dotenv

load_dotenv()

# ntfy priority strings; we map our levels onto them.
_NTFY_PRIORITY = {"info": "default", "warning": "high", "critical": "urgent"}
_PUSHOVER_PRIORITY = {"info": 0, "warning": 1, "critical": 2}


def _ntfy(title: str, body: str, level: str) -> bool:
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return False
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    # HTTP headers must be latin-1; the title may contain em-dashes/symbols, so
    # coerce it to ASCII. The body (UTF-8) keeps the original characters.
    safe_title = title.encode("ascii", "replace").decode("ascii")
    r = requests.post(
        f"{server}/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": safe_title,
            "Priority": _NTFY_PRIORITY.get(level, "high"),
            "Tags": "rotating_light" if level == "critical" else "warning",
        },
        timeout=10,
    )
    return r.ok


def _pushover(title: str, body: str, level: str) -> bool:
    token, user = os.getenv("PUSHOVER_TOKEN"), os.getenv("PUSHOVER_USER")
    if not (token and user):
        return False
    r = requests.post(
        "https://api.pushover.net/1/messages.json",
        data={
            "token": token, "user": user, "title": title, "message": body,
            "priority": _PUSHOVER_PRIORITY.get(level, 1),
        },
        timeout=10,
    )
    return r.ok


def _email(title: str, body: str, level: str) -> bool:
    to = os.getenv("ALERT_EMAIL_TO")
    host, user, pw = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    if not (to and host and user and pw):
        return False
    msg = EmailMessage()
    msg["Subject"] = f"[claude-portfolio:{level}] {title}"
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    return True


def send(title: str, body: str, level: str = "warning") -> list[str]:
    """Fan an alert out to every configured channel. level: info|warning|critical.
    Returns names of channels that delivered. Never raises."""
    delivered = []
    for name, fn in (("ntfy", _ntfy), ("pushover", _pushover), ("email", _email)):
        try:
            if fn(title, body, level):
                delivered.append(name)
        except Exception as e:  # one bad channel must not block the others
            print(f"[notify] {name} failed: {str(e)[:120]}")
    if not delivered:
        # Last-resort: at least make it visible in the run log.
        print(f"[notify] NO CHANNEL CONFIGURED — alert not pushed: {title}\n{body}")
    return delivered
