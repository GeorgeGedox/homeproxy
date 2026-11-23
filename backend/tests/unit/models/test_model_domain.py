"""The in-zone check that routes, records, certificates and Cloudflare share."""

import pytest

from homeproxy.models.domain import in_zone


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("example.com", True, id="apex"),
        pytest.param("a.b.example.com", True, id="below"),
        pytest.param("myexample.com", False, id="same-suffix-other-zone"),
    ],
)
def test_in_zone(name: str, expected: bool) -> None:
    assert in_zone(name, "example.com") is expected
