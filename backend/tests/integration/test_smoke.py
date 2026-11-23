"""Smoke test of the built image. The `stack` fixture already waits for the API's healthz."""

import httpx2


def test_haproxy_serves_the_placeholder(http_url: str) -> None:
    """HAProxy serves the first-boot placeholder page on the published HTTP port."""
    response = httpx2.get(f"{http_url}/", timeout=5.0)
    assert response.status_code == 200
    assert "HomeProxy" in response.text
