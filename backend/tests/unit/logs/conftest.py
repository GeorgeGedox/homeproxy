"""A log listener that raises is only logged by the collector, so a broken one would hide."""

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def no_failed_listener(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    yield
    failed = [r for r in caplog.get_records("call") if r.getMessage() == "a log listener failed"]
    assert failed == []
