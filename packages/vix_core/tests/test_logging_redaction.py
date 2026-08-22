"""Security regression tests: log redaction must never leak secrets."""

import logging

import structlog
from vix_core.logging import RedactingFilter, configure_logging, redact_text


def test_redact_text_masks_kv_pairs() -> None:
    # Synthetic credentials only - NEVER use real account values in tests.
    text = "connected with login=11112222 and password=Not-A-Real-Pw! token=fake-token-abc123"
    redacted = redact_text(text)
    assert "11112222" not in redacted
    assert "Not-A-Real-Pw!" not in redacted
    assert "fake-token-abc123" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_handles_colon_style() -> None:
    text = "account: 12345 balance: 9999.99"
    redacted = redact_text(text)
    assert "12345" not in redacted
    assert "9999.99" not in redacted


def test_structlog_event_dict_redaction() -> None:
    configure_logging("test-service", level="INFO")
    logger = structlog.get_logger("test")
    event_dict = {"event": "login", "mt5_login": 123456, "password": "x", "ok": True}
    from vix_core.logging import redact_event_dict

    scrubbed = redact_event_dict(logger, "info", event_dict)
    assert scrubbed["mt5_login"] == "[REDACTED]"
    assert scrubbed["password"] == "[REDACTED]"
    assert scrubbed["ok"] is True


def test_stdlib_filter_redacts_record(capsys: object) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    root = logging.getLogger("redaction-test")
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    root.info("session for account=987654 started")

    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "987654" not in err
    assert "[REDACTED]" in err


def test_nested_mapping_redaction() -> None:
    from vix_core.logging import _redact_value

    payload = {
        "user": "trader",
        "credentials": {"password": "hunter2", "token": "tok"},
        "notes": ["balance: 5000"],
    }
    result = _redact_value(payload)
    assert result["credentials"]["password"] == "[REDACTED]"
    assert result["credentials"]["token"] == "[REDACTED]"
    assert "5000" not in result["notes"][0]
