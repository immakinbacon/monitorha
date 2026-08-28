"""Update platform.

RouterOS can be upgraded through its API, so those entities support install.
Proxmox exposes no apt-upgrade endpoint and Supermicro publishes no firmware
feed, so those report versions only — the add-on says which is which via
`can_install`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MonitorConfigEntry, MonitorCoordinator
from .entity import MonitorEntity, async_setup_dynamic_entities
from .models import UpdateReading

# Home Assistant rejects release summaries longer than this.
_SUMMARY_LIMIT = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_setup_dynamic_entities(entry, async_add_entities, "updates", MonitorUpdate)


class MonitorUpdate(MonitorEntity, UpdateEntity):
    """Installed-versus-available version for one component."""

    _collection_name = "updates"

    def __init__(
        self,
        coordinator: MonitorCoordinator,
        source_id: str,
        key: str,
        reading: UpdateReading,
    ) -> None:
        super().__init__(coordinator, source_id, key, reading.device_key)
        self._attr_name = reading.name
        self._attr_title = reading.title
        self._attr_entity_category = reading.entity_category
        self._attr_entity_registry_enabled_default = reading.enabled_default
        self._attr_supported_features = (
            UpdateEntityFeature.INSTALL
            if reading.can_install
            else UpdateEntityFeature(0)
        )

    @property
    def installed_version(self) -> str | None:
        item: UpdateReading | None = self._item
        return None if item is None else item.installed_version

    @property
    def latest_version(self) -> str | None:
        item: UpdateReading | None = self._item
        return None if item is None else item.latest_version

    @property
    def release_url(self) -> str | None:
        item: UpdateReading | None = self._item
        return None if item is None else item.release_url

    @property
    def release_summary(self) -> str | None:
        item: UpdateReading | None = self._item
        if item is None or not item.release_summary:
            return None
        return item.release_summary[:_SUMMARY_LIMIT]

    @property
    def in_progress(self) -> bool:
        item: UpdateReading | None = self._item
        return bool(item and item.in_progress)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        item: UpdateReading | None = self._item
        if item is None or not item.can_install:
            raise HomeAssistantError(
                f"{self.entity_id} cannot be installed from Home Assistant"
            )
        await self._async_act("update")
        # The device usually reboots to apply, so the next successful poll of
        # the add-on is what clears the in-progress flag.
        await self.coordinator.async_request_refresh()
