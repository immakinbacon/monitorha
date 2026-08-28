"""Diagnostics support."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN
from .coordinator import MonitorConfigEntry

TO_REDACT = {
    CONF_TOKEN,
    "serial_number",
    "serial",
    "fingerprint",
    "mac_address",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MonitorConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "last_update_success": coordinator.last_update_success,
        "sources": async_redact_data(
            {
                source_id: asdict(source)
                for source_id, source in coordinator.data.sources.items()
            },
            TO_REDACT,
        ),
    }
