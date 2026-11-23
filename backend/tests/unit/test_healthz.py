"""Tests for the liveness endpoint."""

from fastapi.testclient import TestClient

from homeproxy.main import app


def test_healthz_returns_ok() -> None:
    """GET /api/v1/healthz answers 200 with status ok and needs no credentials."""
    response = TestClient(app).get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_under_api_v1_only() -> None:
    """The endpoint is not exposed outside the versioned prefix."""
    assert TestClient(app).get("/healthz").status_code == 404
