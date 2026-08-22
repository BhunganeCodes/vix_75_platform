"""Correlation-id propagation through the gateway (dedicated suite)."""

from __future__ import annotations

import httpx
import pytest
from app.main import build_app
from fakes import FakeRedis
from fastapi.testclient import TestClient
from vix_core.config import Settings

GATEWAY_USER = "vix-admin"
GATEWAY_PASS = "unit-test-gateway-pass"


@pytest.fixture()
def harness() -> tuple[TestClient, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"correlation_id": request.headers.get("X-Correlation-Id")},
            request=request,
        )

    settings = Settings(
        service_name="api-gateway-test",
        gateway_username=GATEWAY_USER,
        gateway_password=GATEWAY_PASS,
        rate_limit_per_minute=10_000,
        jwt_secret="unit-test-secret",
    )
    app = build_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        redis_client=FakeRedis(),
    )
    client = TestClient(app)
    client.post("/token", json={"username": GATEWAY_USER, "password": GATEWAY_PASS})
    return client, captured


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestCorrelationPropagation:
    def test_provided_id_forwarded_verbatim(
        self, harness: tuple[TestClient, list[httpx.Request]]
    ) -> None:
        client, captured = harness
        response = client.get(
            "/signals/history",
            headers={**_auth(_token(client)), "X-Correlation-Id": "trace-provided-42"},
        )
        assert response.status_code == 200
        assert len(captured) == 1
        forwarded = captured[0].headers["X-Correlation-Id"]
        assert forwarded == "trace-provided-42"

    def test_missing_id_generated_and_forwarded(
        self, harness: tuple[TestClient, list[httpx.Request]]
    ) -> None:
        client, captured = harness
        first = client.get("/signals/history", headers=_auth(_token(client)))
        second = client.get("/signals/history", headers=_auth(_token(client)))

        cid1 = captured[0].headers["X-Correlation-Id"]
        cid2 = captured[1].headers["X-Correlation-Id"]

        # Generated (32-hex uuid4 shape), unique per request, echoed to caller.
        for cid in (cid1, cid2):
            assert len(cid) == 32
            assert all(c in "0123456789abcdef" for c in cid)
        assert cid1 != cid2

        assert first.headers["X-Correlation-Id"] == cid1
        assert second.headers["X-Correlation-Id"] == cid2

    def test_error_responses_carry_correlation_too(
        self, harness: tuple[TestClient, list[httpx.Request]]
    ) -> None:
        client, _ = harness
        response = client.get("/signals/history")  # no auth -> 401
        assert response.status_code == 401
        cid = response.headers["X-Correlation-Id"]
        assert len(cid) == 32


def _token(client: TestClient) -> str:
    response = client.post("/token", json={"username": GATEWAY_USER, "password": GATEWAY_PASS})
    assert response.status_code == 200
    return str(response.json()["access_token"])
