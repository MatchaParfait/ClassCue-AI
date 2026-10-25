"""Shared test defaults for the local HTTP API."""

from __future__ import annotations

import starlette.testclient

# The production request boundary only trusts real loopback host names, so
# implicit TestClient bases must not use the fictional http://testserver.
_LOOPBACK_BASE_URL = "http://127.0.0.1:8765"
_testclient_init = starlette.testclient.TestClient.__init__


def _loopback_default_init(self, app, *args, **kwargs):
    # Only the implicit default is replaced; base_url given positionally or
    # by keyword reaches the original constructor untouched.
    if not args and kwargs.get("base_url") is None:
        kwargs["base_url"] = _LOOPBACK_BASE_URL
    _testclient_init(self, app, *args, **kwargs)


starlette.testclient.TestClient.__init__ = _loopback_default_init
