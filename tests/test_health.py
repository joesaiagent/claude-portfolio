"""Offline tests for the health watchdog + notifier (no network, no broker).

We strip all alert-channel env vars so notify.send() takes its no-op path
(nothing is actually pushed during tests)."""
import os

from agents import health, notify
from agents.health import Issue

# Strip channel env vars AFTER import (notify.load_dotenv() at import time would
# otherwise repopulate them from .env). send() reads os.getenv at CALL time, so
# popping here guarantees its no-op path and that tests never push for real.
for _k in ("NTFY_TOPIC", "NTFY_SERVER", "PUSHOVER_TOKEN", "PUSHOVER_USER",
           "ALERT_EMAIL_TO", "SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
    os.environ.pop(_k, None)


def test_notify_noop_without_channels():
    # No channels configured -> delivers nowhere, returns [], never raises.
    assert notify.send("t", "b", level="critical") == []


def test_alertable_filters_healed_and_info():
    issues = [
        Issue("info", "fd_limit", "raised", healed=True),
        Issue("critical", "broker", "down"),
        Issue("warning", "data", "flaky"),
    ]
    bad = health._alertable(issues)
    assert len(bad) == 2
    assert all(not i.healed for i in bad)
    assert {i.severity for i in bad} == {"critical", "warning"}


def test_report_no_push_when_all_healed():
    # Only an auto-healed issue -> nothing to escalate -> returns False.
    assert health.report("premarket/preflight",
                         [Issue("info", "fd_limit", "raised", healed=True)]) is False


def test_report_pushes_on_unhealed():
    # A real warning escalates (send is a no-op here, but report returns True).
    assert health.report("premarket", [Issue("warning", "data", "flaky")]) is True


def test_postflight_flags_step_failure():
    issues = health.postflight("midday",
                               [{"name": "tracker", "error": "boom"}],
                               started_iso="2099-01-01T00:00:00+00:00")
    assert any(i.severity == "critical" and "tracker" in i.where for i in issues)


def test_issue_str_format():
    assert "CRITICAL" in str(Issue("critical", "broker", "down"))
    assert "healed" in str(Issue("info", "fd", "x", healed=True))


def run():
    test_notify_noop_without_channels()
    test_alertable_filters_healed_and_info()
    test_report_no_push_when_all_healed()
    test_report_pushes_on_unhealed()
    test_postflight_flags_step_failure()
    test_issue_str_format()
