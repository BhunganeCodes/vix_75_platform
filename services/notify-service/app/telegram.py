"""Telegram Bot API sender (httpx only - no heavy SDK).

Delivery is BEST-EFFORT: a failed send is logged and returned as False so
lifecycle recording never depends on Telegram availability. The configured
token may be the inert truncated placeholder from .env; failures surface
in logs without crashing the consumer.
"""

from __future__ import annotations

import html

import httpx
from vix_core.config import Settings
from vix_core.logging import get_logger

logger = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramSender:
    """Thin async wrapper around sendMessage with clean formatting."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self.sent = 0
        self.failed = 0

    @property
    def configured(self) -> bool:
        """True when the token looks complete (truncated placeholder fails)."""
        token = self._settings.telegram_token.get_secret_value()
        chat_id = self._settings.telegram_chat_id
        return bool(token) and "..." not in token and chat_id.isdigit()

    # ------------------------------------------------------------------

    async def send(self, text: str) -> bool:
        if not self.configured:
            logger.debug("telegram unconfigured; alert skipped", chars=len(text))
            return False
        token = self._settings.telegram_token.get_secret_value()
        try:
            response = await self._client.post(
                f"/bot{token}/sendMessage",
                json={
                    "chat_id": self._settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self.failed += 1
            logger.warning("telegram delivery failed", error=str(exc))
            return False
        else:
            self.sent += 1
            return True

    # ------------------------------------------------------------------
    # Formatters (pure; exported for unit tests)
    # ------------------------------------------------------------------

    @staticmethod
    def format_generated(payload: dict[str, object]) -> str:
        e = html.escape
        regime = payload.get("regime") or payload.get("regime_label") or "—"
        p_win = payload.get("p_win")
        p_win_s = f"{float(p_win):.2f}" if isinstance(p_win, (int, float)) else "—"
        tf = payload.get("timeframe") or payload.get("ltf_timeframe") or "—"
        symbol = e(str(payload.get("symbol", "—")))
        direction = e(str(payload.get("direction", "—")))
        score = payload.get("score", "—")
        max_score = payload.get("max_score", 7)
        entry = payload.get("entry", "—")
        sl = payload.get("sl", "—")
        tp1 = payload.get("tp1", "—")
        tp2 = payload.get("tp2", "—")
        return (
            f"🟡 <b>SIGNAL GENERATED</b>\n"
            f"Symbol: {symbol}\n"
            f"Direction: {direction} | TF: {e(str(tf))}\n"
            f"Score: {score}/{max_score}\n"
            f"Regime: {e(str(regime))} | P(win): {p_win_s}\n"
            f"Entry: {entry}\n"
            f"SL: {sl} | TP1: {tp1} | TP2: {tp2}"
        )

    @staticmethod
    def format_filled(payload: dict[str, object]) -> str:
        e = html.escape
        ticket = payload.get("ticket", "—")
        symbol = e(str(payload.get("symbol", "—")))
        direction = e(str(payload.get("direction", "—")))
        lots = payload.get("lots", "—")
        fill_price = payload.get("fill_price") or payload.get("entry") or "—"
        sl = payload.get("sl", "—")
        tp = payload.get("tp", "—")
        return (
            f"🟢 <b>ORDER FILLED</b>\n"
            f"Ticket #{ticket}\n"
            f"Symbol: {symbol}\n"
            f"Direction: {direction} | Lots: {lots}\n"
            f"Entry: {fill_price}\n"
            f"SL: {sl} | TP: {tp}"
        )

    @staticmethod
    def format_rejected(payload: dict[str, object]) -> str:
        e = html.escape
        reason = e(str(payload.get("rejected_reason", "—")))
        symbol = e(str(payload.get("symbol", "—")))
        signal_id = e(str(payload.get("signal_id", "—")))
        detail = str(payload.get("detail", ""))[:200]
        return (
            f"🔴 <b>ORDER REJECTED</b>\n"
            f"Reason: {reason}\n"
            f"Symbol: {symbol}\n"
            f"Signal: {signal_id}\n"
            f"Detail: {e(detail)}"
        )

    @staticmethod
    def format_closed(payload: dict[str, object]) -> str:
        e = html.escape
        pnl = payload.get("pnl", 0.0)
        arrow = "📈" if isinstance(pnl, (int, float)) and pnl >= 0 else "📉"
        ticket = payload.get("ticket", "—")
        symbol = e(str(payload.get("symbol", "—")))
        exit_price = payload.get("exit_price", "—")
        return (
            f"🔵 <b>TRADE CLOSED</b> {arrow}\n"
            f"PnL: {pnl}\n"
            f"Ticket #{ticket} | Symbol: {symbol}\n"
            f"Exit: {exit_price}"
        )
