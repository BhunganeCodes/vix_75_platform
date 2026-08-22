"""MT5 order execution with retcode classification and bounded retries.

Isolation contract: every raw ``MetaTrader5`` call happens through the
injected ``mt5`` module-like object (resolved lazily from
``vix_core.mt5_client.require_mt5`` in production). Tests substitute a
fake module - no monkeypatching of package internals needed.

Retcode policy (audit finding fix - the legacy EA never inspected these):

* DONE / DONE_PARTIAL          -> filled, terminal.
* REQUOTE, PRICE_CHANGED, PRICE_OFF, TIMEOUT -> retryable, exponential
  backoff, max 3 attempts total.
* everything else (INVALID_VOLUME, NO_MONEY, INVALID_STOPS, ...) ->
  hard failure: log ``mt5.last_error()``, emit rejection, NEVER retry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from vix_core.config import Settings
from vix_core.logging import get_logger
from vix_core.mt5_client import MT5Client
from vix_core.schemas import Direction, OrderRequest, OrderResult

logger = get_logger(__name__)

# Documented MetaTrader5 trade server return codes (stable numeric values).
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_NO_MONEY = 10019
RETRYABLE_RETCODES: frozenset[int] = frozenset(
    {
        10004,  # TRADE_RETCODE_REQUOTE
        10020,  # TRADE_RETCODE_PRICE_CHANGED
        10021,  # TRADE_RETCODE_PRICE_OFF
        10012,  # TRADE_RETCODE_TIMEOUT
        10018,  # TRADE_RETCODE_MARKET_CLOSED (transient across sessions)
    }
)

MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    """Full execution attempt history around an OrderResult."""

    result: OrderResult
    attempts: int = 1
    retryable_gave_up: bool = False
    last_error: str | None = None


@dataclass(slots=True)
class MT5Executor:
    """Synchronous MT5 caller; consumers offload via asyncio.to_thread."""

    settings: Settings
    client: MT5Client | None = None
    mt5: Any | None = None  # injected module-like object (tests/fakes)
    backoff_base_s: float = 0.5
    _send_calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = MT5Client(self.settings)
        if self.mt5 is None:
            from vix_core.mt5_client import require_mt5

            self.mt5 = require_mt5()

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------

    def build_request(self, order: OrderRequest, *, price: float | None = None) -> dict[str, Any]:
        mt5 = self._module()
        order_type = mt5.ORDER_TYPE_BUY if order.direction is Direction.BUY else mt5.ORDER_TYPE_SELL
        tick = self._tick(order.symbol)
        deal_price = price or (tick.ask if order.direction is Direction.BUY else tick.bid)
        return {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(order.lots),
            "type": order_type,
            "price": float(deal_price),
            "sl": float(order.sl),
            "tp": float(order.tp),
            "deviation": int(order.deviation_points),
            "magic": int(self.settings.mt5_magic),
            "comment": f"vix75:{order.idempotency_key[:16]}",
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 1),
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1),
        }

    def _module(self) -> Any:
        assert self.mt5 is not None  # noqa: S101 - set in __post_init__
        return self.mt5

    def _tick(self, symbol: str) -> Any:
        mt5 = self._module()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"symbol_info_tick returned None for {symbol}")
        return tick

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, order: OrderRequest, *, correlation_id: str = "-") -> ExecOutcome:
        """Send the market order; classify retcode; retry transients."""
        request = self.build_request(order)
        mt5 = self._module()

        attempts = 0
        delay = self.backoff_base_s
        last_desc = "no response"
        last_error: str | None = None

        while attempts < MAX_ATTEMPTS:
            attempts += 1
            self._send_calls.append(dict(request))
            result = mt5.order_send(request)
            if result is None:
                last_error = str(mt5.last_error())
                last_desc = f"order_send returned None ({last_error})"
                logger.error(
                    "order_send none",
                    key=order.idempotency_key,
                    attempt=attempts,
                    error=last_error,
                )
            else:
                retcode = int(result.retcode)
                last_desc = _describe_retcode(retcode, getattr(result, "comment", ""))

                if retcode in (TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL):
                    logger.info(
                        "order filled",
                        key=order.idempotency_key,
                        ticket=int(result.order),
                        price=float(result.price),
                        attempts=attempts,
                        correlation_id=correlation_id,
                    )
                    return ExecOutcome(
                        result=OrderResult(
                            idempotency_key=order.idempotency_key,
                            accepted=True,
                            retcode=retcode,
                            retcode_description=last_desc,
                            ticket=int(result.order),
                            price=float(result.price),
                        ),
                        attempts=attempts,
                        last_error=last_error,
                    )

                if retcode not in RETRYABLE_RETCODES:
                    raw_error = str(mt5.last_error())
                    logger.error(
                        "order hard-failed (no retry)",
                        key=order.idempotency_key,
                        retcode=retcode,
                        description=last_desc,
                        mt5_last_error=raw_error,
                        correlation_id=correlation_id,
                    )
                    return ExecOutcome(
                        result=_rejected(order, retcode, last_desc),
                        attempts=attempts,
                        last_error=raw_error,
                    )

                last_error = str(mt5.last_error())
                logger.warning(
                    "retryable retcode; backing off",
                    key=order.idempotency_key,
                    retcode=retcode,
                    description=last_desc,
                    attempt=attempts,
                    delay_s=delay,
                )

            if attempts < MAX_ATTEMPTS:
                time.sleep(delay)
                delay *= 2

        logger.error(
            "retries exhausted",
            key=order.idempotency_key,
            attempts=attempts,
            description=last_desc,
        )
        return ExecOutcome(
            result=_rejected(order, None, f"RETRY_EXHAUSTED after {attempts}: {last_desc}"),
            attempts=attempts,
            retryable_gave_up=True,
            last_error=last_error,
        )

    # ------------------------------------------------------------------
    # Position close (routes + reconciliation support)
    # ------------------------------------------------------------------

    def close_position(self, symbol: str, ticket: int, volume: float) -> ExecOutcome:
        mt5 = self._module()
        positions = mt5.positions_get(ticket=ticket) or ()
        if not positions:
            return ExecOutcome(
                result=OrderResult(
                    idempotency_key=f"close:{ticket}",
                    accepted=False,
                    retcode_description="POSITION_NOT_FOUND",
                ),
                attempts=1,
            )
        pos = positions[0]
        is_buy = getattr(pos, "type", 0) == getattr(mt5, "POSITION_TYPE_BUY", 0)
        tick = self._tick(symbol)
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        price = tick.bid if is_buy else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": close_type,
            "position": int(ticket),
            "price": float(price),
            "deviation": 50,
            "magic": int(self.settings.mt5_magic),
            "comment": f"vix75-close:{ticket}",
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1),
        }
        result = mt5.order_send(request)
        if result is None:
            return ExecOutcome(
                result=OrderResult(
                    idempotency_key=f"close:{ticket}",
                    accepted=False,
                    retcode_description=f"ORDER_SEND_NONE:{mt5.last_error()}",
                ),
                attempts=1,
            )
        retcode = int(result.retcode)
        accepted = retcode == TRADE_RETCODE_DONE
        return ExecOutcome(
            result=OrderResult(
                idempotency_key=f"close:{ticket}",
                accepted=accepted,
                retcode=retcode,
                retcode_description=_describe_retcode(retcode, getattr(result, "comment", "")),
                ticket=int(getattr(result, "order", 0) or ticket),
                price=float(getattr(result, "price", 0.0) or 0.0),
            ),
            attempts=1,
        )


def _rejected(order: OrderRequest, retcode: int | None, desc: str) -> OrderResult:
    return OrderResult(
        idempotency_key=order.idempotency_key,
        accepted=False,
        retcode=retcode,
        retcode_description=desc,
    )


_DESCRIPTIONS: dict[int, str] = {
    10004: "REQUOTE",
    10006: "REJECT",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10012: "TIMEOUT",
    10013: "INVALID_REQUEST",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
}


def _describe_retcode(retcode: int, comment: str) -> str:
    base = _DESCRIPTIONS.get(retcode, f"RETCODE_{retcode}")
    return f"{base}" + (f":{comment}" if comment else "")
