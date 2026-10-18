import logging

from bgmu_bot.__main__ import RedactingFormatter, main


def test_missing_token_is_an_actionable_startup_error(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main() == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_logging_redacts_sensitive_values_even_in_exception_messages():
    formatter = RedactingFormatter(["fake-token", "fake-password"])
    try:
        raise RuntimeError("fake-token fake-password")
    except RuntimeError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Request for %s", ("fake-token",), sys.exc_info())
        result = formatter.format(record)
    assert "fake-token" not in result
    assert "fake-password" not in result
    assert "[REDACTED]" in result
