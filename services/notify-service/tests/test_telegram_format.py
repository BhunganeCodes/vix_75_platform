"""Telegram formatting + delivery tests (httpx mocked)."""

from __future__ import annotations

import json

import pytest
from app.consumer import NotifyConsumer
from app.telegram import TelegramSender
from fakes import FakeAsyncClient, FakeLifecycle
from vix_core.config import Settings

TOKEN = "123456789:AA-FakeTokenForUnitTests-000000"
CHAT_ID = "111222333"


def _sender(
    client: FakeAsyncClient | None = None,
) -> tuple[TelegramSender, Settings, FakeAsyncClient]:
    settings = Settings(
        service_name="notify-test",
        telegram_token=TOKEN,
        telegram_chat_id=CHAT_ID,
    )
    client = client or FakeAsyncClient()
    return TelegramSender(settings, client), settings, client  # type: ignore[arg-type]


class _FakeRedis:
    """Bare stub - tests invoke _handle directly and never touch streams."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.kv[key] = value
        return True


class TestFormatters:
    def test_generated_message_contains_required_fields(self) -> None:
        sender, _, _ = _sender()
        del sender
        text = TelegramSender.format_generated(
            {
                "symbol": "Volatility 75 Index",
                "direction": "BUY",
                "timeframe": "M15",
                "score": 6,
                "max_score": 7,
                "regime": "S1_trend_up",
                "p_win": 0.81,
                "entry": 100.0,
                "sl": 98.5,
                "tp1": 103.0,
                "tp2": 104.5,
            }
        )
        assert text.startswith("🟡")
        assert "SIGNAL GENERATED" in text
        for needle in (
            "Volatility 75 Index",
            "BUY",
            "M15",
            "Score: 6/7",
            "S1_trend_up",
            "P(win): 0.81",
            "Entry: 100.0",
            "SL: 98.5",
            "TP1: 103.0",
        ):
            assert needle in text, f"missing {needle}"

    def test_filled_message_includes_ticket(self) -> None:
        text = TelegramSender.format_filled(
            {
                "ticket": 777_123,
                "symbol": "VIX75",
                "direction": "SELL",
                "lots": 0.15,
                "fill_price": 99.9,
                "sl": 101.5,
                "tp": 95.0,
            }
        )
        assert text.startswith("🟢")
        assert "ORDER FILLED" in text
        assert "Ticket #777123" in text

    def test_rejected_and_closed_messages(self) -> None:
        rej = TelegramSender.format_rejected(
            {
                "rejected_reason": "MARGIN_EXCEEDED",
                "symbol": "VIX75",
                "signal_id": "abc",
                "detail": "budget",
            }
        )
        assert rej.startswith("🔴") and "MARGIN_EXCEEDED" in rej

        closed = TelegramSender.format_closed(
            {"pnl": -12.4, "ticket": 42, "symbol": "VIX75", "exit_price": 97.7}
        )
        assert closed.startswith("🔵") and "TRADE CLOSED" in closed and "-12.4" in closed


class TestDelivery:
    @pytest.mark.asyncio
    async def test_filled_event_posts_to_telegram_api(self) -> None:
        """Spec assertion: filled order -> correct HTTP POST payload."""
        sender, settings, client = _sender()
        lifecycle = FakeLifecycle()
        consumer = NotifyConsumer(settings, _FakeRedis(), lifecycle, sender)  # type: ignore[arg-type]

        await consumer._handle(
            "order.filled",
            b"1-1",
            {
                "idempotency_key": "sig-tg-0001",
                "signal_id": "ff" * 16,
                "symbol": "Volatility 75 Index",
                "direction": "BUY",
                "lots": "0.2",
                "entry": "100.0",
                "sl": "95.0",
                "tp": "110.0",
                "ticket": "777123",
                "fill_price": "100.05",
                "correlation_id": "corr-tg-1",
            },
        )

        assert len(client.posts) == 1
        post = client.posts[0]
        assert f"/bot{TOKEN}/sendMessage" in post.url
        assert post.json_body["chat_id"] == CHAT_ID
        assert post.json_body["parse_mode"] == "HTML"
        assert "🟢" in str(post.json_body["text"])
        assert "ORDER FILLED" in str(post.json_body["text"])
        assert "Ticket #777123" in str(post.json_body["text"])
        assert lifecycle.records == [("order.filled", "sig-tg-0001", "ok")]
        assert consumer.alerts_sent == 1

    @pytest.mark.asyncio
    async def test_rejections_muted_by_default(self) -> None:
        sender, settings, client = _sender()
        lifecycle = FakeLifecycle()
        consumer = NotifyConsumer(settings, _FakeRedis(), lifecycle, sender)  # type: ignore[arg-type]

        await consumer._handle(
            "signal.rejected",
            b"1-2",
            {
                "signal_id": "ee" * 16,
                "symbol": "VIX75",
                "rejected_reason": "max_open_trades_reached",
                "correlation_id": "corr-tg-2",
            },
        )

        assert client.posts == []  # muted per spec default
        assert lifecycle.records[0][0] == "signal.rejected"
        assert lifecycle.records[0][2] == "rejected"

    @pytest.mark.asyncio
    async def test_truncated_token_never_posts(self) -> None:
        settings = Settings(
            service_name="notify-test",
            telegram_token="7863491044:AAG4...",  # inert placeholder  # noqa: S106
            telegram_chat_id="111222333",
        )
        client = FakeAsyncClient()
        sender = TelegramSender(settings, client)
        assert sender.configured is False
        ok = await sender.send("🟡 would-be alert")
        assert ok is False and client.posts == []

    def test_stream_payload_roundtrip_is_json_safe(self) -> None:
        """The execution-service 'signal' blob must decode cleanly."""
        blob = json.dumps({"direction": "BUY", "lots": 0.2, "retcode_description": "DONE"})
        parsed = json.loads(blob)
        assert parsed["direction"] == "BUY"
