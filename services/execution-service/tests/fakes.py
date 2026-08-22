"""Shared test doubles: fake MetaTrader5 module + fake Redis.

The fake MT5 module mirrors the surface execution code touches
(order_send, symbol_info_tick, positions_get, history_deals_get,
last_error and the numeric constants) so tests inject it directly into
MT5Executor - no monkeypatching required.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_NO_MONEY = 10019


@dataclass
class FakeTick:
    bid: float = 100.0
    ask: float = 100.1


@dataclass
class FakeSendResult:
    retcode: int
    order: int = 0
    price: float = 0.0
    comment: str = ""
    volume: float = 0.0


@dataclass
class FakePosition:
    ticket: int
    symbol: str = "Volatility 75 Index"
    volume: float = 0.2
    price_open: float = 100.0
    profit: float = 12.5
    type: int = 0


@dataclass
class FakeDeal:
    ticket: int = 2
    entry: int = 1  # DEAL_ENTRY_OUT
    price: float = 103.0
    profit: float = 60.0
    volume: float = 0.2


class FakeMt5Module:
    """Scriptable stand-in for the raw MetaTrader5 package."""

    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    ORDER_TIME_GTC = 1
    ORDER_FILLING_IOC = 1

    def __init__(self, send_results: list[Any] | None = None) -> None:
        self.send_results: list[Any] = list(send_results or [])
        self.order_send_calls: list[dict[str, Any]] = []
        self.positions: list[FakePosition] = []
        self.deals: dict[int, list[FakeDeal]] = {}
        self._err = (0, "trade operation succeeded")

    # ---- scripting helpers -------------------------------------------

    def queue(self, *results: Any) -> None:
        self.send_results.extend(results)

    def last_error(self) -> tuple[int, str]:
        return self._err

    # ---- API surface ---------------------------------------------------

    def symbol_info_tick(self, symbol: str) -> FakeTick:
        return FakeTick()

    def order_send(self, request: dict[str, Any]) -> Any:
        self.order_send_calls.append(dict(request))
        if not self.send_results:
            raise AssertionError("order_send called with an empty script")
        item = self.send_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def positions_get(self, **kwargs: Any) -> list[FakePosition]:
        ticket = kwargs.get("ticket")
        if ticket is not None:
            return [p for p in self.positions if p.ticket == int(ticket)]
        return list(self.positions)

    def history_deals_get(
        self, date_from: Any, date_to: Any, *, position: int = 0
    ) -> list[FakeDeal]:
        return list(self.deals.get(int(position), []))


def filled_result(ticket: int = 777_001, price: float = 100.05) -> FakeSendResult:
    return FakeSendResult(retcode=TRADE_RETCODE_DONE, order=ticket, price=price, comment="ok")


def make_order(**overrides: Any) -> dict[str, Any]:
    base = {
        "idempotency_key": "sig-test-0001",
        "signal_id": "a" * 32,
        "symbol": "Volatility 75 Index",
        "direction": "BUY",
        "lots": 0.2,
        "entry": 100.0,
        "sl": 95.0,
        "tp": 110.0,
        "deviation_points": 30,
    }
    base.update(overrides)
    return base


class FakeRedis:
    """Minimal async Redis: streams w/ consumer groups + KV (SET/GET)."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: set[tuple[str, str]] = set()
        self.delivered: set[tuple[str, str, str]] = set()
        self.kv: dict[str, tuple[str, int | None]] = {}
        self._seq = 0
        self._clock = int(time.time() * 1000)

    # -- streams ---------------------------------------------------------

    async def xgroup_create(
        self, stream: str, group: str, id: str = "0", mkstream: bool = False
    ) -> None:
        self.groups.add((stream, group))
        self.streams.setdefault(stream, [])

    async def xadd(self, stream: str, fields: dict[str, Any]) -> str:
        self._clock += 1
        self._seq += 1
        entry_id = f"{self._clock}-{self._seq}"
        encoded = {k: (v if isinstance(v, str) else json_dumps(v)) for k, v in fields.items()}
        self.streams.setdefault(stream, []).append((entry_id, encoded))
        return entry_id

    async def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: dict[str, str],
        count: int = 10,
        block: int = 0,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        result: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        for stream in streams:
            pending: list[tuple[str, dict[str, str]]] = []
            for eid, fields in self.streams.get(stream, []):
                marker = (stream, group, eid)
                if marker not in self.delivered and len(pending) < count:
                    self.delivered.add(marker)
                    pending.append((eid, fields))
            if pending:
                result.append((stream, pending))
        return result

    async def xack(self, stream: str, group: str, *ids: str) -> int:
        return len(ids)

    def entries(self, stream: str) -> list[tuple[str, dict[str, str]]]:
        return list(self.streams.get(stream, []))

    # -- kv ----------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        item = self.kv.get(key)
        return item[0] if item else None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.kv[key] = (value, ex)
        return True


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)


class FakeExecutionDB:
    """In-memory ExecutionDatabase double tracking lifecycle calls."""

    def __init__(self) -> None:
        self.pending_keys: set[str] = set()
        self.statuses: dict[str, str] = {}
        self.filled_meta: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, dict[str, Any]] = {}

    async def insert_pending(self, order: Any) -> bool:
        key = order.idempotency_key
        if key in self.pending_keys:
            return False
        self.pending_keys.add(key)
        self.statuses[key] = "pending"
        self.rows[key] = {
            "idempotency_key": key,
            "symbol": order.symbol,
            "side": str(order.direction),
            "lots": order.lots,
        }
        return True

    async def set_status(
        self,
        status: str,
        key: str,
        *,
        retcode: int | None = None,
        desc: str | None = None,
        ticket: int | None = None,
        price: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.statuses[key] = status
        if status == "filled":
            self.filled_meta[key] = {"ticket": ticket, "price": price}

    async def audit(self, *args: Any, **kwargs: Any) -> None:
        pass


@dataclass
class BackoffProbe:
    """Records elapsed wall-time between retries (kept for future use)."""

    stamps: list[float] = field(default_factory=list)

    def mark(self) -> None:
        self.stamps.append(time.monotonic())
