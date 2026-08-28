"""Supervisor integration: self-discovery so the HA integration auto-configures.

Publishing a discovery record makes Home Assistant raise a config flow for the
`monitorha` integration with this add-on's hostname, port and API token already
filled in. Older Supervisor builds validated the `service` name against a fixed
list and will reject a custom one; that is not fatal, the integration can
always be pointed at the add-on by hand.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_API = "http://supervisor"
DISCOVERY_SERVICE = "monitorha"


class Supervisor:
    """Thin client for the Supervisor API available inside an add-on."""

    def __init__(self) -> None:
        self._token = os.environ.get("SUPERVISOR_TOKEN", "")

    @property
    def available(self) -> bool:
        return bool(self._token)

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.request(
                    method, f"{SUPERVISOR_API}{path}", headers=headers, json=payload
                ) as response,
            ):
                body = await response.json(content_type=None)
                if response.status >= 400:
                    _LOGGER.debug(
                        "Supervisor %s %s -> HTTP %s: %s",
                        method,
                        path,
                        response.status,
                        body,
                    )
                    return None
                return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Supervisor %s %s failed: %s", method, path, err)
            return None

    async def async_hostname(self) -> str | None:
        """The add-on's own DNS name on the Supervisor network."""
        info = await self._request("GET", "/addons/self/info")
        if not info:
            return None
        return (info.get("data") or {}).get("hostname")

    async def async_publish_discovery(self, port: int, token: str) -> bool:
        """Offer this add-on to Home Assistant's discovery."""
        if not self.available:
            return False
        hostname = await self.async_hostname()
        if not hostname:
            return False
        result = await self._request(
            "POST",
            "/discovery",
            {
                "service": DISCOVERY_SERVICE,
                "config": {"host": hostname, "port": port, "token": token},
            },
        )
        if result is None:
            _LOGGER.info(
                "Supervisor did not accept the discovery record; add the "
                "integration manually using host %s port %s",
                hostname,
                port,
            )
            return False
        _LOGGER.info("Published discovery for %s:%s", hostname, port)
        return True
