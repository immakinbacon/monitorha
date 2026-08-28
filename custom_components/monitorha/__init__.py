"""Infrastructure Monitor: native entities for the monitoring add-on."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import MonitorConfigEntry, MonitorCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: MonitorConfigEntry) -> bool:
    """Connect to the add-on and publish everything it is monitoring."""
    coordinator = MonitorCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MonitorConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: MonitorConfigEntry) -> None:
    """Apply changed options by rebuilding the coordinator."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: MonitorConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting devices the add-on no longer reports.

    Proxmox guests get destroyed and sources get removed in the add-on's UI;
    without this the stale device would linger in the registry forever.
    """
    coordinator = entry.runtime_data
    live = {
        (DOMAIN, f"{entry.entry_id}:{source.id}:{device_key}")
        for source in coordinator.data.sources.values()
        for device_key in source.devices
    }
    return not any(identifier in live for identifier in device.identifiers)
