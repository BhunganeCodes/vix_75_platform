"""structlog configuration with mandatory secret redaction.

Rules enforced here (see audit findings):

* No raw account numbers, passwords, tokens, or balances may reach any
  sink - neither through structlog key/value events nor through stdlib
  loggers used by libraries (uvicorn, psycopg, ...).
* ``RedactingFilter`` is attached to the root stdlib handler;
  ``redact_event_dict`` runs inside every structlog pipeline.

Never call ``print()`` in services. Always use
``structlog.get_logger(__name__)``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.typing import Processor

REDACTED = "[REDACTED]"

#: Event-dict keys whose values must never be logged verbatim.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "mt5_login",
        "login",
        "account",
        "account_id",
        "balance",
        "equity",
        "password",
        "passwd",
        "pwd",
        "mt5_password",
        "token",
        "telegram_token",
        "deriv_api_token",
        "api_key",
        "secret",
        "jwt_secret",
        "authorization",
    }
)

# Matches ``key=value`` / ``key: value`` occurrences inside free text.
_KEY_VALUE_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(mt5_login|mt5_password|login|account(?:_id)?|balance|equity|"
    r"password|passwd|pwd|token|secret|api_key)\b(\s*[:=]\s*)(\S+)"
)


def redact_text(text: str) -> str:
    """Redact ``key=value`` style secrets embedded in free-form text."""

    def _sub(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{REDACTED}"

    return _KEY_VALUE_RE.sub(_sub, text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, MutableMapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                redacted[key] = REDACTED
            else:
                redacted[key] = _redact_value(item)
        return redacted
    if isinstance(value, (list, tuple, set)):
        container_type = type(value)
        return container_type(_redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_event_dict(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor: scrub sensitive keys/values from every event."""
    del logger, method  # processor signature requirement
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return _redact_value(event_dict)


class RedactingFilter(logging.Filter):
    """Stdlib filter that scrubs secrets from record messages/args.

    Attach to handlers of any third-party logger that bypasses structlog.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        if record.args:
            args = _redact_value(record.args)
            record.args = tuple(args) if isinstance(args, tuple) else args
        return True


def configure_logging(
    service_name: str,
    *,
    level: str = "INFO",
    json_output: bool = False,
) -> None:
    """Configure structlog AND the stdlib root logger with redaction."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_event_dict,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, psycopg, ...) through the same rules.
    root = logging.getLogger()
    root.setLevel(logging.getLevelNamesMapping().get(level.upper(), logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    handler.addFilter(RedactingFilter())
    root.handlers.clear()
    root.addHandler(handler)

    get_logger(service_name).info("logging configured", service=service_name)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Convenience wrapper so callers never import structlog directly."""
    return structlog.get_logger(name)
