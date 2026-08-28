"""Shared entity base and the dynamic-discovery helper used by every platform."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MonitorConfigEntry, MonitorCoordinator
from .models import SourceData


class MonitorEntity(CoordinatorEntity[MonitorCoordinator]):
    """Base for every entity this integration creates.

    Static metadata (name, device class, unit) is captured once at construction
    so it cannot flap between polls; only the value is read live.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MonitorCoordinator,
        source_id: str,
        key: str,
        device_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._source_id = source_id
        self._key = key
        self._device_key = device_key
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{source_id}_{key}"

        source = coordinator.data.sources.get(source_id)
        meta = source.devices.get(device_key) if source else None
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}:{source_id}:{device_key}")}
        )
        if meta:
            info.update(
                DeviceInfo(
                    name=meta.name,
                    manufacturer=meta.manufacturer,
                    model=meta.model,
                    sw_version=meta.sw_version,
                    hw_version=meta.hw_version,
                    serial_number=meta.serial_number,
                    configuration_url=meta.configuration_url,
                )
            )
            if meta.via_device:
                info["via_device"] = (
                    DOMAIN,
                    f"{entry_id}:{source_id}:{meta.via_device}",
                )
        self._attr_device_info = info

    @property
    def _source(self) -> SourceData | None:
        return self.coordinator.data.sources.get(self._source_id)

    @property
    def _collection_name(self) -> str:
        """Which snapshot collection this entity reads from."""
        raise NotImplementedError

    @property
    def _item(self) -> Any:
        source = self._source
        if source is None:
            return None
        return getattr(source, self._collection_name).get(self._key)

    @property
    def available(self) -> bool:
        source = self._source
        # A source the add-on cannot currently reach goes unavailable, as does
        # an entity that has disappeared from it entirely.
        return (
            super().available
            and source is not None
            and source.available
            and self._item is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        item = self._item
        result = {
            k: v for k, v in (getattr(item, "attributes", None) or {}).items()
            if v is not None
        }
        # A bare "problem" state says nothing about what is wrong, so the
        # explanation travels with the entity as well as in the event.
        for name in ("severity", "reason"):
            value = getattr(item, name, None)
            if value is not None:
                result[name] = value
        return result or None

    async def _async_act(self, kind: str, value: bool | None = None) -> None:
        """Ask the add-on to perform this entity's action."""
        await self.coordinator.client.async_action(
            self._source_id, kind, self._key, value
        )


@callback
def async_setup_dynamic_entities(
    entry: MonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
    collection: str,
    factory: Callable[[MonitorCoordinator, str, str, Any], MonitorEntity],
) -> None:
    """Create entities for a snapshot collection, including ones found later.

    Which entities exist is only known after the add-on has polled: fans,
    Proxmox guests and interfaces all vary per device, sources can be added in
    the add-on's UI at any time, and a source that is down reports nothing at
    all until it recovers. So this re-checks on every refresh.
    """
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _discover() -> None:
        new = []
        for (source_id, key), item in coordinator.data.iter_items(collection):
            if (source_id, key) in known:
                continue
            known.add((source_id, key))
            new.append(factory(coordinator, source_id, key, item))
        if new:
            async_add_entities(new)

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))
