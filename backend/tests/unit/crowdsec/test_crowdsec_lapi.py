"""LAPI client checks that need no LAPI. `tests/integration/crowdsec/` drives a real one."""

from datetime import timedelta

import pytest

from homeproxy.crowdsec import LapiClient
from homeproxy.util.redact import redact

# Nothing listens on the discard port.
CLOSED = "http://127.0.0.1:9"


@pytest.mark.parametrize(
    ("value", "kind", "duration"),
    [
        ("198.51.100.1", "throttle", timedelta(hours=1)),
        ("example.com", "ban", timedelta(hours=1)),
        ("::/0", "ban", timedelta(hours=1)),
        ("::ffff:198.51.100.1", "ban", timedelta(hours=1)),
        ("198.51.100.1", "ban", timedelta(milliseconds=500)),
    ],
    ids=["other-kind", "not-an-address", "catch-all", "ipv4-mapped", "under-a-second"],
)
def test_a_bad_decision_is_refused_before_anything_is_sent(
    value: str, kind: str, duration: timedelta
) -> None:
    client = LapiClient(CLOSED, "homeproxy-ui", "a-password")
    with pytest.raises(ValueError):  # a sent request would raise `LapiError`
        client.add_decision(value, kind, duration)  # type: ignore[arg-type]


def test_the_password_is_redacted() -> None:
    LapiClient(CLOSED, "homeproxy-ui", "lapi-machine-password")
    assert redact("login with lapi-machine-password") == "login with [REDACTED]"
