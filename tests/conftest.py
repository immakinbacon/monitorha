"""Test helpers: a fake aiohttp session that replays canned API responses."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Provides the `hass` and `enable_custom_integrations` fixtures used by the
# end-to-end tests; the backend tests do not need it.
pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Let Home Assistant load this integration from custom_components."""
    if "hass" not in request.fixturenames:
        yield
        return
    request.getfixturevalue("enable_custom_integrations")
    yield


class Status:
    """Route marker for replying with a specific HTTP status."""

    def __init__(
        self, code: int, payload: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        self.code = code
        self.payload = payload if payload is not None else {"error": f"HTTP {code}"}
        self.headers = headers or {}


class Raw:
    """Route marker for a body that is not JSON.

    SwOS answers with JavaScript object notation, so those routes have to
    reach the client as text rather than being re-encoded as JSON.
    """

    def __init__(self, text: str) -> None:
        self.text = text


class FakeResponse:
    """Minimal stand-in for `aiohttp.ClientResponse`."""

    def __init__(
        self, status: int, payload: Any, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self._payload = None if isinstance(payload, Raw) else payload
        if isinstance(payload, Raw):
            self._body = payload.text
        else:
            self._body = "" if payload is None else json.dumps(payload)
        self.content_length = len(self._body)
        self.headers = headers or {}

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def release(self) -> None:
        """aiohttp releases a redirect response before following it."""

    async def json(self, content_type: str | None = None) -> Any:
        if self._payload is None and self._body:
            raise ValueError("body is not JSON")
        return self._payload

    async def text(self) -> str:
        return self._body


class FakeSession:
    """Routes requests to a {(method, path): payload} table.

    A route whose value is a callable is invoked with the request headers, so
    a test can vary the reply by how the caller authenticated.

    Paths are matched on `path?query` so query strings are significant.  A
    missing route answers 404, which the clients treat as "not supported here".
    """

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    async def close(self) -> None:
        """Match aiohttp's session interface; nothing to release here."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Any = None,
        auth: Any = None,
        json: Any = None,
        data: Any = None,
        timeout: Any = None,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        parsed = urlparse(url)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        self.calls.append((method.upper(), path, json or data))

        key = (method.upper(), path)
        if key in self.routes:
            payload = self.routes[key]
            if callable(payload) and not isinstance(payload, type):
                payload = payload(headers or {})
            if isinstance(payload, Exception):
                raise payload
            if isinstance(payload, Status):
                return FakeResponse(payload.code, payload.payload, payload.headers)
            return FakeResponse(200, payload)
        if key[0] in ("POST", "PATCH"):
            # Actions that were not explicitly stubbed still succeed.
            return FakeResponse(204, None)
        return FakeResponse(404, {"error": "not found"})


@pytest.fixture
def make_session():
    def _make(routes: dict[tuple[str, str], Any]) -> FakeSession:
        return FakeSession(routes)

    return _make
