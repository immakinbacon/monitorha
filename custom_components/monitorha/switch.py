"""Switch platform: chassis power, guest power and PoE-out ports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .coordinator import MonitorConfigEntry, MonitorCoordinator
from .entity import MonitorEntity, async_setup_dynamic_entities
from .models import SwitchSpec

# A chassis or guest takes a while to reach its new power state, and the add-on
# only learns about it on its next poll of the device, so follow-up refreshes
# are scheduled rather than relying on the immediate one alone.
_SETTLE_DELAYS = (8, 30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_setup_dynamic_entities(entry, async_add_entities, "switches", MonitorSwitch)


class MonitorSwitch(MonitorEntity, SwitchEntity):
    """A two-state control on a monitored device."""

    _collection_name = "switches"

    def __init__(
        self,
        coordinator: MonitorCoordinator,
        source_id: str,
        key: str,
        spec: SwitchSpec,
    ) -> None:
        super().__init__(coordinator, source_id, key, spec.device_key)
        self._attr_name = spec.name
        self._attr_device_class = spec.device_class
        self._attr_icon = spec.icon
        self._attr_entity_category = spec.entity_category
        self._attr_entity_registry_enabled_default = spec.enabled_default
        self._attr_assumed_state = spec.assumed_state
        self._optimistic: bool | None = None
        self._settle_timers: list[Callable[[], None]] = []

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        spec: SwitchSpec | None = self._item
        return None if spec is None else spec.value

    @callback
    def _handle_coordinator_update(self) -> None:
        # Once a poll reports the requested state, stop overriding it.
        spec: SwitchSpec | None = self._item
        if spec is not None and spec.value == self._optimistic:
            self._optimistic = None
        super()._handle_coordinator_update()

    async def _async_set(self, on: bool) -> None:
        await self._async_act("switch", on)
        self._optimistic = on
        self.async_write_ha_state()
        self._schedule_settle_refresh()

    async def _async_settle_refresh(self, _now: datetime) -> None:
        """Re-poll after the device has had time to change state.

        Defined as a coroutine method so Home Assistant schedules it as an
        async job; a lambda returning a coroutine would never be awaited.
        """
        await self.coordinator.async_request_refresh()

    @callback
    def _schedule_settle_refresh(self) -> None:
        self._cancel_settle_timers()
        self._settle_timers = [
            async_call_later(self.hass, delay, self._async_settle_refresh)
            for delay in _SETTLE_DELAYS
        ]

    @callback
    def _cancel_settle_timers(self) -> None:
        for cancel in self._settle_timers:
            cancel()
        self._settle_timers = []

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_settle_timers()
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
