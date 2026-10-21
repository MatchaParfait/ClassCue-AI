"""Bounded local diagnostics; secrets are removed after traceback formatting."""

from __future__ import annotations

from classcue.paths import PROJECT_ROOT

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
from threading import RLock
import traceback
from classcue.asr_storage import log_directory


LOG_DIR = log_directory(PROJECT_ROOT)
_LOCK = RLock()
_SECRETS: set[str] = set()
_HANDLER: RotatingFileHandler | None = None
_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*['\"]?(?:bearer\s+)?)[^\s,'\"}]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token)\s*['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
)


def register_secret(value: str | None) -> None:
    if value and value.strip():
        with _LOCK:
            _SECRETS.add(value.strip())


def redact_text(value: object) -> str:
    text = str(value)
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "HF_TOKEN"):
        secret = os.getenv(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = redact_text(super().format(record))
        if len(text) > 64 * 1024:
            text = text[:64 * 1024] + "\n[diagnostic truncated]"
        return text


class _RedactRecord(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)).rstrip())
            record.exc_info = None
        return True


def configure_logging(log_dir: Path | None = None) -> Path:
    global _HANDLER
    with _LOCK:
        if _HANDLER is None:
            from scripts.launcher_process import RedactingRotatingFileHandler

            directory = log_dir or LOG_DIR
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"backend-{os.getpid()}.log"
            _HANDLER = RedactingRotatingFileHandler(
                path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            _HANDLER.setFormatter(RedactingFormatter(
                "%(asctime)s %(levelname)s pid=%(process)d %(name)s %(message)s",
            ))
            root = logging.getLogger("classroom")
            root.setLevel(logging.INFO)
            root.addHandler(_HANDLER)
        return Path(_HANDLER.baseFilename)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"classroom.{name}")
    if not any(isinstance(item, _RedactRecord) for item in logger.filters):
        logger.addFilter(_RedactRecord())
    return logger


def log_exception(
    logger: logging.Logger, message: str, *, exc: BaseException | None = None, **context,
) -> None:
    details = " ".join(f"{key}={redact_text(value)!r}" for key, value in context.items())
    exception_info = (type(exc), exc, exc.__traceback__) if exc is not None else True
    logger.error("%s %s", message, details, exc_info=exception_info)
