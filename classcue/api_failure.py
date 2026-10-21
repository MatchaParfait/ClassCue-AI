"""Preserve machine-readable API failures across sanitized exception boundaries."""

from __future__ import annotations


class ApiUnavailable(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def api_failure(error: BaseException) -> BaseException | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if (
            isinstance(current, (ApiUnavailable, ConnectionError, TimeoutError))
            or type(current).__name__ in {"APIConnectionError", "APITimeoutError"}
            or isinstance(status, int) and (
                status in {400, 401, 403, 404, 405, 408, 422, 429} or status >= 500
            )
        ):
            return current
        current = current.__cause__ or current.__context__
    return None
