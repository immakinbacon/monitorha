"""Client for the add-on, and the coordinator that polls it."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    EVENT_MONITOR,
)
from .models import AddonData

_LOGGER = logging.getLogger(__name__)

type MonitorConfigEntry = ConfigEntry[MonitorCoordinator]


class AddonAuthError(HomeAssistantError):
    """The add-on rejected our API token."""


class AddonConnectionError(HomeAssistantError):
    """The add-on could not be reached."""


class AddonClient:
    """Talks to the add-on's HTTP API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str,
    ) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)

    async def _request(self, method: str, path: str, payload: Any = None) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                json=payload,
                timeout=self._timeout,
            ) as response:
                if response.status == 401:
                    raise AddonAuthError("The add-on rejected the API token")
                body = await response.json(content_type=None)
                if response.status >= 400:
                    message = (body or {}).get("error", f"HTTP {response.status}")
                    raise AddonConnectionError(message)
                return body
        except aiohttp.ClientError as err:
            raise AddonConnectionError(f"Cannot reach the add-on: {err}") from err
        except TimeoutError as err:
            raise AddonConnectionError("Timed out talking to the add-on") from err

    async def async_health(self) -> dict[str, Any]:
        return await self._request("GET", "/api/health")

    async def async_snapshot(self) -> dict[str, Any]:
        return await self._request("GET", "/api/snapshot")

    async def async_events(self, since: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/events?since={since}")

    async def async_action(
        self, source_id: str, kind: str, key: str, value: bool | None = None
    ) -> None:
        await self._request(
            "POST",
            "/api/action",
            {"source_id": source_id, "kind": kind, "key": key, "value": value},
        )


class MonitorCoordinator(TimestampDataUpdateCoordinator[AddonData]):
    """Reads the add-on's snapshot.

    This poll is local and cheap; the add-on is what actually talks to the
    monitored hardware, on its own per-source schedule.
    """

    config_entry: MonitorConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MonitorConfigEntry) -> None:
        self.client = AddonClient(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_TOKEN],
        )
        interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        # None until the first poll has read the add-on's current position.
        # Starting at 0 instead would replay the add-on's whole buffer on every
        # Home Assistant restart and fire automations for changes that are
        # already history.
        self._event_cursor: int | None = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )

    async def _async_update_data(self) -> AddonData:
        try:
            raw = await self.client.async_snapshot()
        except AddonAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AddonConnectionError as err:
            raise UpdateFailed(str(err)) from err
        await self._async_dispatch_events()
        return AddonData.from_json(raw)

    async def _async_dispatch_events(self) -> None:
        """Republish the add-on's change events on the Home Assistant bus.

        Failing to read them must not fail the whole poll: the entities are
        still perfectly good, and events resume on the next cycle from the same
        cursor, so nothing is silently dropped.
        """
        try:
            payload = await self.client.async_events(self._event_cursor or 0)
        except (AddonAuthError, AddonConnectionError) as err:
            _LOGGER.debug("Could not read events from the add-on: %s", err)
            return

        head = int(payload.get("head", 0))
        if self._event_cursor is None:
            # First poll of this config entry: adopt the add-on's position
            # without firing, then report everything from here on.
            self._event_cursor = head
            return

        for event in payload.get("events") or []:
            self.hass.bus.async_fire(EVENT_MONITOR, event)
            self._event_cursor = max(self._event_cursor, int(event.get("seq", 0)))
