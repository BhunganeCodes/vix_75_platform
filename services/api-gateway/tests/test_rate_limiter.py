"""Rate limiting: 100/min per subject; the 101st request gets a 429."""

from __future__ import annotations

import httpx
import pytest
from app.main import build_app
from fakes import FakeRedis
from fastapi.testclient import TestClient
from vix_core.config import Settings

GATEWAY_USER = "vix-admin"
GATEWAY_PASS = "unit-test-gateway-pass"


def _app(limit: int) -> TestClient:
    settings = Settings(
        service_name="api-gateway-test",
        gateway_username=GATEWAY_USER,
        gateway_password=GATEWAY_PASS,
        rate_limit_per_minute=limit,
        jwt_secret="unit-test-secret",
    )

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"correlation_id": request.headers.get("X-Correlation-Id")},
            request=request,
        )

    app = build_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)),
        redis_client=FakeRedis(),
    )
    return TestClient(app)


def _token(client: TestClient) -> str:
    response = client.post("/token", json={"username": GATEWAY_USER, "password": GATEWAY_PASS})
    assert response.status_code == 200
    return str(response.json()["access_token"])


class TestSlidingWindow:
    def test_101st_request_returns_429(self) -> None:
        client = _app(limit=100)
        token = _token(client)
        headers = {"Authorization": f"Bearer {token}"}

        codes: list[int] = []
        for i in range(101):
            response = client.get("/signals/history", headers=headers)
            codes.append(response.status_code)
            if i < 100:
                assert response.status_code == 200, f"request {i + 1} unexpectedly blocked"

        assert codes[:100] == [200] * 100
        last = client.get("/signals/history", headers=headers)
        assert codes[-1] == 429 or last.status_code == 429
        if last.status_code == 429:
            assert "Retry-After" in last.headers
            assert last.json()["detail"] == "rate limit exceeded"

    def test_different_subjects_have_independent_windows(self) -> None:
        client = _app(limit=3)

        t1 = _token(client)
        # Second subject: same creds would yield the SAME sub, so mint a
        # distinct identity directly - windows must key off `sub`.
        from app.auth import create_token

        t2, _ = create_token(Settings(jwt_secret="unit-test-secret"), subject="other-user")

        for _ in range(3):
            assert (
                client.get(
                    "/signals/history", headers={"Authorization": f"Bearer {t1}"}
                ).status_code
                == 200
            )
        assert (
            client.get("/signals/history", headers={"Authorization": f"Bearer {t1}"}).status_code
            == 429
        )

        # Subject 2 has its own window.
        assert (
            client.get("/signals/history", headers={"Authorization": f"Bearer {t2}"}).status_code
            == 200
        )


class TestLimiterUnit:
    @pytest.mark.asyncio
    async def test_decision_counts_and_remaining(self) -> None:
        from app.rate_limiter import SlidingWindowLimiter

        limiter = SlidingWindowLimiter(FakeRedis(), limit_per_window=3, window_seconds=60)
        decisions = [await limiter.check("alice") for _ in range(5)]

        allowed_flags = [d.allowed for d in decisions]
        assert allowed_flags == [True, True, True, False, False]

        remaining_after_first = decisions[0].remaining
        assert remaining_after_first == 2

    @pytest.mark.asyncio
    async def test_window_prunes_old_entries(self) -> None:
        """Entries older than the window no longer count (sliding, not fixed)."""
        from app.rate_limiter import SlidingWindowLimiter
        from fakes import FakeRedis

        fake = FakeRedis()
        # Seed an entry from the far past directly.
        await fake.zadd("rate:bob", {"ancient-request": 1_000})

        limiter = SlidingWindowLimiter(fake, limit_per_window=1, window_seconds=60)
        decision = await limiter.check("bob")  # ancient member pruned first

        assert decision.allowed is True
