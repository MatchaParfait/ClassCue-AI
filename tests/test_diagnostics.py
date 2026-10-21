import logging
from logging.handlers import RotatingFileHandler

from diagnostics import RedactingFormatter, get_logger, log_exception, redact_text, register_secret


def test_redaction_covers_keys_headers_env_and_traceback(tmp_path, monkeypatch):
    register_secret("private-test-key-value")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-test-key")
    path = tmp_path / "diagnostic.log"
    handler = RotatingFileHandler(path, maxBytes=2048, backupCount=2, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    logger = get_logger("redaction_test")
    logger.addHandler(handler)
    try:
        try:
            raise ValueError("private-test-key-value environment-test-key Authorization: Bearer abcdefg")
        except ValueError as exc:
            log_exception(logger, "Failure", exc=exc, job_id="job1")
        text = path.read_text(encoding="utf-8")
        assert "private-test-key-value" not in text
        assert "environment-test-key" not in text
        assert "abcdefg" not in text
        assert "ValueError" in text and "Traceback" in text and "job1" in text
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_rotation_remains_bounded(tmp_path):
    path = tmp_path / "bounded.log"
    handler = RotatingFileHandler(path, maxBytes=512, backupCount=2, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.getLogger("rotation_test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for _ in range(100):
            logger.info("x" * 100)
        files = list(tmp_path.glob("bounded.log*"))
        assert len(files) == 3
        assert all(item.stat().st_size <= 512 for item in files)
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_common_key_shapes_are_redacted():
    text = redact_text('{"api_key": "key-value", "access_token": "token-value"} sk-longsecrettest')
    assert "key-value" not in text
    assert "token-value" not in text
    assert "sk-longsecrettest" not in text


def test_oversized_diagnostic_is_truncated_after_redaction():
    register_secret("oversized-test-secret")
    record = logging.LogRecord("test", logging.ERROR, "", 1, "oversized-test-secret" + "x" * 100000, (), None)
    text = RedactingFormatter("%(message)s").format(record)
    assert len(text) < 66000
    assert "oversized-test-secret" not in text
    assert "diagnostic truncated" in text
