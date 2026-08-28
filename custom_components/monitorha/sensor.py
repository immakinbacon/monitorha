"""Sensor platform."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, StateType
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MonitorConfigEntry, MonitorCoordinator
from .entity import MonitorEntity, async_setup_dynamic_entities
from .models import Reading


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_setup_dynamic_entities(entry, async_add_entities, "sensors", MonitorSensor)


class MonitorSensor(MonitorEntity, SensorEntity):
    """A single value read from a monitored device."""

    _collection_name = "sensors"

    def __init__(
        self,
        coordinator: MonitorCoordinator,
        source_id: str,
        key: str,
        reading: Reading,
    ) -> None:
        super().__init__(coordinator, source_id, key, reading.device_key)
        self._attr_name = reading.name
        self._attr_device_class = reading.device_class
        self._attr_state_class = reading.state_class
        self._attr_native_unit_of_measurement = reading.unit
        self._attr_icon = reading.icon
        self._attr_entity_category = reading.entity_category
        self._attr_entity_registry_enabled_default = reading.enabled_default
        self._attr_options = reading.options
        self._attr_suggested_display_precision = reading.suggested_display_precision

    @property
    def native_value(self) -> StateType | Any:
        item: Reading | None = self._item
        if item is None:
            return None
        value = item.value
        if (
            self._attr_device_class is SensorDeviceClass.ENUM
            and self._attr_options is not None
            and value not in self._attr_options
        ):
            # Firmware reporting an out-of-spec enum value would otherwise spam
            # the log on every poll.
            return None
        return value
