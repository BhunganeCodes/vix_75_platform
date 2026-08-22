"""Gateway auth + proxy + correlation-id tests.

The downstream internal service is MOCKED via httpx.MockTransport; the
rate limiter runs against an in-memory fake (a Redis-container variant
lives in test_rate_limiter.py).
"""

from __future__ import annotations

import json

import httpx
import pytest
from app.main import build_app
from fakes import FakeRedis
from fastapi.testclient import TestClient
from vix_core.config import Settings

GATEWAY_USER = "vix-admin"
GATEWAY_PASS = "unit-test-gateway-pass"


@pytest.fixture()
def captured() -> list[httpx.Request]:
    return []


@pytest.fixture()
def client(captured: list[httpx.Request]) -> TestClient:
    def downstream_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "echo_path": request.url.path,
                "correlation_id": request.headers.get("X-Correlation-Id"),
                "auth": request.headers.get("Authorization"),
            },
            request=request,
        )

    transport = httpx.MockTransport(downstream_handler)
    settings = Settings(
        service_name="api-gateway-test",
        gateway_username=GATEWAY_USER,
        gateway_password=GATEWAY_PASS,
        rate_limit_per_minute=10_000,  # not under test here
        jwt_secret="unit-test-secret",
    )
    app = build_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(downstream_handler)),
        redis_client=FakeRedis(),
    )
    del transport
    return TestClient(app)


def _login(client: TestClient) -> str:
    response = client.post("/token", json={"username": GATEWAY_USER, "password": GATEWAY_PASS})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    return str(body["access_token"])


class TestAuth:
    def test_missing_token_returns_401(self, client: TestClient) -> None:
        response = client.get("/signals/history")
        assert response.status_code == 401
        assert "detail" in response.json()

    def test_garbage_token_returns_401(self, client: TestClient) -> None:
        response = client.get("/signals/history", headers={"Authorization": "Bearer not-a-jwt"})
        assert response.status_code == 401

    def test_token_endpoint_issues_valid_jwt(self, client: TestClient) -> None:
        token = _login(client)
        assert token.count(".") == 2  # header.payload.signature

    def test_token_with_bad_credentials_rejected(self, client: TestClient) -> None:
        response = client.post("/token", json={"username": GATEWAY_USER, "password": "wrong"})
        assert response.status_code == 401


class TestProxying:
    def test_valid_jwt_proxies_to_mocked_service(
        self, client: TestClient, captured: list[httpx.Request]
    ) -> None:
        token = _login(client)
        response = client.get(
            "/signals/history?limit=5",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["echo_path"] == "/history"  # prefix stripped
        assert any(r.url.host == "signal-service" for r in captured)

    def test_unknown_service_404(self, client: TestClient) -> None:
        token = _login(client)
        response = client.get("/nonexistent/whatever", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 404

    def test_post_body_forwarded(self, client: TestClient, captured: list[httpx.Request]) -> None:
        token = _login(client)
        payload = {"lots": 0.2, "symbol": "Volatility 75 Index"}
        response = client.post(
            "/risk/size",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        forwarded = json.loads(captured[-1].content.decode())
        assert forwarded == payload


class TestCorrelationId:
    def test_incoming_header_propagated_downstream(
        self, client: TestClient, captured: list[httpx.Request]
    ) -> None:
        token = _login(client)
        response = client.get(
            "/signals/history",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Correlation-Id": "my-trace-123",
            },
        )
        assert response.status_code == 200
        assert captured[-1].headers["X-Correlation-Id"] == "my-trace-123"
        assert response.headers["X-Correlation-Id"] == "my-trace-123"

    def test_absent_header_generated_uuid(
        self, client: TestClient, captured: list[httpx.Request]
    ) -> None:
        token = _login(client)
        response = client.get("/signals/history", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        cid = captured[-1].headers["X-Correlation-Id"]
        # uuid4 hex = 32 chars
        assert len(cid) == 32 and all(c in "0123456789abcdef" for c in cid)
        assert response.headers["X-Correlation-Id"] == cid
