"""Binary sensor platform."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MonitorConfigEntry, MonitorCoordinator
from .entity import MonitorEntity, async_setup_dynamic_entities
from .models import BinaryReading


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_setup_dynamic_entities(
        entry, async_add_entities, "binary_sensors", MonitorBinarySensor
    )


class MonitorBinarySensor(MonitorEntity, BinarySensorEntity):
    """A health, link or running state."""

    _collection_name = "binary_sensors"

    def __init__(
        self,
        coordinator: MonitorCoordinator,
        source_id: str,
        key: str,
        reading: BinaryReading,
    ) -> None:
        super().__init__(coordinator, source_id, key, reading.device_key)
        self._attr_name = reading.name
        self._attr_device_class = reading.device_class
        self._attr_icon = reading.icon
        self._attr_entity_category = reading.entity_category
        self._attr_entity_registry_enabled_default = reading.enabled_default

    @property
    def is_on(self) -> bool | None:
        item: BinaryReading | None = self._item
        return None if item is None else item.value
