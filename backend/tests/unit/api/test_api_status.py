"""`/status` and `/audit` on the real app, with no HAProxy running."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.haproxy.version_check import VersionStatus
from homeproxy.jobs.update_check import UpdateCheck, UpdateStatus
from homeproxy.store import Store
from tests.unit.api.conftest import FakeLapi, enable_crowdsec


def test_status_answers_without_haproxy_and_reports_the_last_apply(
    data_dir: Path, api: TestClient
) -> None:
    """With HAProxy down, the status still answers. The last apply is the newest `apply` audit
    entry, and each version check is the last result found. With nothing applied, the DNS server
    has no address to listen on, so its running process reports no uptime."""
    audit = Store(data_dir).audit
    audit.append("admin", "apply", {"success": True, "stage": "done", "snapshot": "s1"})
    audit.append(
        "admin",
        "apply",
        {
            "success": False,
            "stage": "check",
            "snapshot": None,
            "errors": ["haproxy -c failed"],
            "warnings": ["example.com is routed only on /test"],
        },
    )
    audit.append("admin", "login")
    found = VersionStatus(
        running="3.4.7",
        branch="3.4",
        latest="3.4.9",
        eol=date(2031, 4, 1),
        checked_at=datetime(2026, 11, 10, tzinfo=UTC),
    )
    api.app.state.version_check.status = found  # type: ignore[attr-defined]
    update = UpdateStatus(
        running="0.1.0",
        latest="0.2.0",
        update_available=True,
        checked_at=datetime(2026, 11, 10, tzinfo=UTC),
    )
    check = UpdateCheck("file:///nonexistent")
    check.status = update
    api.app.state.update_check = check  # type: ignore[attr-defined]
    pid_file = data_dir / "dns.pid"
    pid_file.write_text("1\n")
    services = api.app.state.services  # type: ignore[attr-defined]
    pipeline = replace(services.pipeline, dns_pid_file=pid_file)
    api.app.state.services = replace(services, pipeline=pipeline)  # type: ignore[attr-defined]

    status = api.get("/api/v1/status").json()

    assert status["haproxy_version"] is None
    assert status["haproxy_error"]
    assert status["haproxy_uptime_seconds"] is None
    assert status["dns_listening"] is False
    assert status["dns_uptime_seconds"] is None
    assert status["routes"] == []
    assert status["bouncer"] is None
    assert VersionStatus.model_validate(status["version_check"]) == found
    assert UpdateStatus.model_validate(status["update_check"]) == update
    assert status["update_check_configured"] is True
    last = status["last_apply"]
    assert (last["success"], last["stage"], last["snapshot_id"], last["errors"]) == (
        False,
        "check",
        None,
        ["haproxy -c failed"],
    )
    assert last["warnings"] == ["example.com is routed only on /test"]


@pytest.mark.parametrize(
    ("setup", "state"),
    [
        pytest.param({"appsec": True}, "up", id="answers"),
        pytest.param({"appsec": True, "bouncer_key": "wrong"}, "down", id="refuses-the-key"),
        pytest.param({}, None, id="off"),
    ],
)
def test_status_reports_whether_appsec_checks_requests_for_the_bouncer(
    data_dir: Path,
    lapi: tuple[FakeLapi, str],
    api: TestClient,
    setup: dict[str, Any],
    state: str | None,
) -> None:
    """AppSec fails open, so a stopped AppSec shows only here and in its alert."""
    enable_crowdsec(data_dir, lapi[1], **setup)

    assert api.get("/api/v1/status").json()["appsec"] == state


def test_status_reuses_the_appsec_reading_until_the_settings_change(
    data_dir: Path, lapi: tuple[FakeLapi, str], api: TestClient
) -> None:
    """Each page of the dashboard asks for `/status`, and an AppSec that does not answer takes
    seconds to time out. A save of the settings shows at once."""
    enable_crowdsec(data_dir, lapi[1], appsec=True)
    assert api.get("/api/v1/status").json()["appsec"] == "up"

    lapi[0].appsec_refuses = True
    assert api.get("/api/v1/status").json()["appsec"] == "up"

    secrets = data_dir / "secrets" / "secrets.yaml"
    secrets.write_text(secrets.read_text())
    assert api.get("/api/v1/status").json()["appsec"] == "down"


def test_status_has_no_last_apply_before_the_first(api: TestClient) -> None:
    """A new install has applied nothing and checked no version yet. With no release source, the
    HomeProxy update check says it is not configured."""
    status = api.get("/api/v1/status").json()

    assert (status["last_apply"], status["version_check"], status["update_check"]) == (
        None,
        None,
        None,
    )
    assert status["update_check_configured"] is False


def test_the_audit_log_is_returned_newest_first(data_dir: Path, api: TestClient) -> None:
    """`limit` caps the number of entries."""
    audit = Store(data_dir).audit
    for snapshot in ("s1", "s2", "s3"):
        audit.append("admin", "apply", {"snapshot": snapshot})

    entries = api.get("/api/v1/audit", params={"limit": 2}).json()

    assert [(e["user"], e["action"], e["details"]) for e in entries] == [
        ("admin", "apply", {"snapshot": "s3"}),
        ("admin", "apply", {"snapshot": "s2"}),
    ]
