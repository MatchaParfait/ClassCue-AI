"""Shared cooperative cancellation signal for local processing tasks."""

from __future__ import annotations


class ProcessingCancelled(RuntimeError):
    """Raised when a user requests a running local task to stop."""


class ResourcePaused(RuntimeError):
    """Recoverable resource failure; do not decode again or advance the queue."""

    def __init__(self, message, *, cache_directory=None, failure=None):
        super().__init__(message)
        self.cache_directory = str(cache_directory or "")
        self.failure = failure or {}
        self.resume_request = None
