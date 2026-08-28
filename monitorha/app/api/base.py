"""Shared plumbing for the HTTP backends."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from ..const import DEFAULT_TIMEOUT
from ..models import Snapshot

_LOGGER = logging.getLogger(__name__)


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """Scheme, host and port, with the scheme's default port filled in.

    `urlsplit().port` is None when the port is implicit, so a redirect from
    `https://host:443/x` to `https://host/x` would otherwise look like a
    different origin. Clients always build URLs with an explicit port, while
    devices routinely omit it in `Location`.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(scheme)


def _same_origin(first: str, second: str) -> bool:
    """True when two URLs share scheme, host and effective port."""
    return _origin(first) == _origin(second)


_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class MonitorError(Exception):
    """Base error for all backends."""


class AuthenticationError(MonitorError):
    """Credentials were rejected."""


class ConnectionFailed(MonitorError):
    """The device could not be reached, or answered with something unusable."""


class BaseClient(ABC):
    """Common HTTP behaviour shared by every backend."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        *,
        use_ssl: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._scheme = "https" if use_ssl else "http"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._boot_times: dict[str, datetime] = {}

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

    @abstractmethod
    async def async_fetch(self, *, slow: bool) -> Snapshot:
        """Poll the device.

        `slow` is True on the occasional deeper poll, for data that is
        expensive to gather or that barely ever changes (available package
        updates, SMART health, backup inventories).
        """

    async def async_validate(self) -> dict[str, Any]:
        """Probe the device during config flow.

        Returns a dict with at least `unique_id` and `title`.
        """
        raise NotImplementedError

    async def async_close(self) -> None:  # noqa: B027
        """Release anything held on the device.

        Deliberately a concrete no-op rather than abstract: only backends that
        hold server-side state need it. A Redfish session token occupies one of
        a BMC's few session slots until released; RouterOS and Proxmox hold
        nothing that outlives the HTTP connection.
        """

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: aiohttp.BasicAuth | None = None,
        json: Any = None,
        data: Any = None,
        allow_status: tuple[int, ...] = (),
        timeout: float | None = None,
        optional: bool = False,
    ) -> Any:
        """Issue a request and decode the JSON body.

        Returns None for empty bodies and for any status listed in
        `allow_status`, so callers can treat "endpoint not supported on this
        firmware" as a soft miss rather than a failure.

        `optional` extends that tolerance to transport failures: a timeout or
        refused connection on a non-essential endpoint returns None instead of
        failing the whole poll. Some endpoints are slow enough to time out on a
        healthy host — Proxmox's disk inventory shells out to smartctl, and a
        cross-node request is proxied through the node you connected to.

        `timeout` overrides the client default for this one request.
        """
        request_timeout = (
            self._timeout if timeout is None else aiohttp.ClientTimeout(total=timeout)
        )
        try:
            resp = await self._send(
                method,
                url,
                headers=headers,
                auth=auth,
                json=json,
                data=data,
                timeout=request_timeout,
            )
        except aiohttp.ClientConnectorCertificateError as err:
            # Always fatal: a rejected certificate is a misconfiguration that
            # would silently disable half the entities if swallowed.
            raise ConnectionFailed(
                f"TLS certificate rejected for {url}. Disable 'Verify SSL "
                f"certificate' if this device uses a self-signed certificate: {err}"
            ) from err
        except aiohttp.ClientError as err:
            if optional:
                _LOGGER.debug("Skipping optional %s: %s", url, err)
                return None
            raise ConnectionFailed(f"Error connecting to {url}: {err}") from err
        except TimeoutError as err:
            if optional:
                _LOGGER.debug("Timeout on optional %s, skipping", url)
                return None
            raise ConnectionFailed(
                f"Timeout after {request_timeout.total}s connecting to {url}"
            ) from err

        async with resp:
            # Checked before the auth statuses: 403 means "authenticated, but
            # not entitled to *this* endpoint", which for an optional endpoint
            # must degrade rather than fail the whole poll. A caller opts in by
            # listing the status in `allow_status`.
            if resp.status in allow_status:
                _LOGGER.debug("Ignoring HTTP %s from %s", resp.status, url)
                return None
            if resp.status == 403:
                raise AuthenticationError(
                    f"Not authorised for {url} (HTTP 403). The account "
                    f"authenticated but lacks a privilege this endpoint needs."
                )
            if resp.status == 401:
                raise AuthenticationError(
                    f"Credentials rejected by {url} (HTTP 401)"
                )
            if resp.status >= 400:
                body = (await resp.text())[:300]
                raise ConnectionFailed(f"HTTP {resp.status} from {url}: {body}")
            if resp.status == 204 or resp.content_length == 0:
                return None
            try:
                return await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise ConnectionFailed(f"Malformed JSON from {url}: {err}") from err

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        auth: aiohttp.BasicAuth | None,
        json: Any,
        data: Any,
        timeout: aiohttp.ClientTimeout,
    ) -> aiohttp.ClientResponse:
        """Issue a request, following redirects without mangling it.

        aiohttp's own redirect handling follows RFC 7231 and rewrites POST to
        GET on 301/302/303, discarding the body. Some Supermicro firmware
        301s every collection URI to its trailing-slash form, which would turn
        a session login or a `ComputerSystem.Reset` into a bodyless GET — the
        request arrives stripped of its credentials or its action and the BMC
        answers 401. Redirects are therefore followed here with the method and
        body intact.
        """
        target = url
        for _ in range(_MAX_REDIRECTS):
            response = await self._session.request(
                method,
                target,
                headers=headers,
                auth=auth,
                json=json,
                data=data,
                timeout=timeout,
                allow_redirects=False,
            )
            if response.status not in _REDIRECT_STATUSES:
                return response

            location = response.headers.get("Location")
            response.release()
            if not location:
                raise ConnectionFailed(
                    f"{target} returned HTTP {response.status} without a Location"
                )

            following = urljoin(target, location)
            if not _same_origin(target, following):
                # Credentials must not be replayed to another host.
                raise ConnectionFailed(
                    f"{target} redirected to a different origin ({following}); "
                    f"refusing to forward credentials"
                )
            _LOGGER.debug("Following redirect %s -> %s", target, following)
            target = following

        raise ConnectionFailed(f"Too many redirects starting at {url}")

    def boot_time(self, key: str, uptime_seconds: float | None) -> datetime | None:
        """Convert an uptime into a stable boot timestamp.

        Recomputing `now - uptime` every poll produces a value that jitters by a
        second or two, which would spam the recorder.  Only accept a new value
        when it drifts far enough to be a genuine reboot rather than rounding.
        """
        if uptime_seconds is None:
            return None
        candidate = datetime.now(UTC) - timedelta(seconds=float(uptime_seconds))
        previous = self._boot_times.get(key)
        if previous is None or abs((candidate - previous).total_seconds()) > 30:
            self._boot_times[key] = candidate
            return candidate
        return previous


def to_float(value: Any) -> float | None:
    """Best-effort numeric coercion; many of these APIs return numbers as text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # RouterOS reports some health values with a trailing unit, e.g. "24.5C".
    while text and not (text[-1].isdigit() or text[-1] == "."):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    result = to_float(value)
    return None if result is None else int(result)


def percent(used: Any, total: Any) -> float | None:
    """Return used/total as a percentage, guarding against zero and None."""
    used_f, total_f = to_float(used), to_float(total)
    if used_f is None or not total_f:
        return None
    return round(used_f / total_f * 100, 2)


def truthy(value: Any) -> bool | None:
    """Interpret the assorted boolean spellings these APIs use."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1", "ok", "okay", "good", "enabled", "running", "up"):
        return True
    # "fail" and friends matter most: a failed PSU must read as a problem
    # rather than as an unknown state.
    if text in (
        "false",
        "no",
        "off",
        "0",
        "disabled",
        "stopped",
        "down",
        "fail",
        "failed",
        "fault",
        "error",
        "critical",
        "bad",
        "absent",
    ):
        return False
    return None
