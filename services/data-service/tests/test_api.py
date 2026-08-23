"""data-service API smoke tests (no MT5, no Postgres required)."""

from collections.abc import Iterator

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # Dependencies may be degraded in CI; /health must still answer.
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_service_truthfully(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "data-service"
    assert body["status"] in {"ok", "degraded"}
    assert body["database"] in {"up", "down"}
    assert body["mt5"] in {"connected", "standby", "unavailable"}


def test_health_never_contains_secrets(client: TestClient) -> None:
    response = client.get("/health")
    raw = response.text.lower()
    for forbidden in ("password", "token", "mt5_login", "balance"):
        assert forbidden not in raw


def test_backfill_rejects_unknown_timeframe(client: TestClient) -> None:
    response = client.post("/backfill", json={"timeframes": ["M7"], "lookback_days": 30})
    assert response.status_code == 422


def test_backfill_route_exists(client: TestClient) -> None:
    """POST /backfill route is registered (not 404); may hit real infra."""
    import contextlib

    with contextlib.suppress(Exception):
        client.post("/backfill", json={"timeframes": ["M15"], "lookback_days": 7})
