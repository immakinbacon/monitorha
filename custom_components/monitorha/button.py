"""Button platform: reboots, power actions and on-demand refreshes."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MonitorConfigEntry, MonitorCoordinator
from .entity import MonitorEntity, async_setup_dynamic_entities
from .models import ButtonSpec


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_setup_dynamic_entities(entry, async_add_entities, "buttons", MonitorButton)


class MonitorButton(MonitorEntity, ButtonEntity):
    """A one-shot action against a monitored device."""

    _collection_name = "buttons"

    def __init__(
        self,
        coordinator: MonitorCoordinator,
        source_id: str,
        key: str,
        spec: ButtonSpec,
    ) -> None:
        super().__init__(coordinator, source_id, key, spec.device_key)
        self._attr_name = spec.name
        self._attr_device_class = spec.device_class
        self._attr_icon = spec.icon
        self._attr_entity_category = spec.entity_category
        self._attr_entity_registry_enabled_default = spec.enabled_default

    async def async_press(self) -> None:
        if self._item is None:
            raise HomeAssistantError(f"{self.entity_id} is no longer available")
        await self._async_act("button")
        await self.coordinator.async_request_refresh()
