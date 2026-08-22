"""X-Correlation-Id generation and propagation helpers.

Every Redis stream message and internal HTTP call carries a correlation id
so a single signal can be traced across data -> feature -> ml -> signal ->
risk -> execution. IDs are bound to the structlog context so every log line
emitted while handling an event is automatically correlated.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import structlog

CORRELATION_HEADER = "X-Correlation-Id"
CORRELATION_FIELD = "correlation_id"

_logger = structlog.get_logger(__name__)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def get_or_create_correlation_id(payload: Mapping[str, object] | None) -> str:
    """Extract the correlation id from a stream payload/headers, else mint one."""
    if payload:
        raw = payload.get(CORRELATION_FIELD) or payload.get(CORRELATION_HEADER)
        if raw:
            return str(raw)
    cid = new_correlation_id()
    _logger.debug("minted correlation id", **{CORRELATION_FIELD: cid})
    return cid


def bind_correlation_id(correlation_id: str) -> None:
    """Attach the correlation id to all subsequent log lines in this context."""
    structlog.contextvars.bind_contextvars(**{CORRELATION_FIELD: correlation_id})


def unbind_correlation_id() -> None:
    structlog.contextvars.unbind_contextvars(CORRELATION_FIELD)


def stream_fields(payload: dict[str, object], correlation_id: str | None = None) -> dict[str, str]:
    """Prepare a Redis stream entry: string fields + correlation id."""
    cid = correlation_id or new_correlation_id()
    fields = {key: str(value) for key, value in payload.items()}
    fields[CORRELATION_FIELD] = cid
    return fields
