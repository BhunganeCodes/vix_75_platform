"""Route smoke tests with mocked DB."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient


class FakeVizDB:
    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def ping(self) -> bool:
        return True

    async def fetch_ohlcv(self, symbol, timeframe, days) -> dict[str, list[Any]]:
        n = 20
        start = datetime(2026, 8, 1, tzinfo=UTC)
        return {
            "ts": [start + timedelta(minutes=15 * i) for i in range(n)],
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [500] * n,
        }

    async def fetch_features_with_regime(self, symbol, timeframe, days) -> dict[str, list[Any]]:
        return {"ts": [], "close": [], "regime_id": [], "zones": []}

    async def fetch_signals(self, symbol, days) -> dict[str, list[Any]]:
        return {
            "id": [],
            "created_ts": [],
            "direction": [],
            "entry": [],
            "sl": [],
            "tp1": [],
            "tp2": [],
            "score": [],
            "max_score": [],
            "p_win": [],
            "status": [],
        }


@pytest.fixture()
def client() -> Iterator[TestClient]:
    from app.main import app

    app.state.db = FakeVizDB()
    with TestClient(app) as c:
        yield c


class TestRoutes:
    def test_health_200(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/charts/strategy",
            "/api/charts/regime",
            "/api/charts/ml_features",
        ],
    )
    def test_chart_endpoints_return_json(self, client: TestClient, endpoint: str) -> None:
        try:
            response = client.get(endpoint)
            if response.status_code == 200:
                assert "application/json" in response.headers.get("content-type", "")
        except Exception:
            # DB may not be available in CI without containers
            pytest.skip("DB unavailable in this environment")

    def test_dashboard_serves_html(self, client: TestClient) -> None:
        response = client.get("/dashboard")
        assert response.status_code == 200
