"""Snapshot <-> JSON.

The add-on and the integration keep identical data models; this module is the
only place that knows how they cross the wire. Callables (`press`, `turn`,
`install`) cannot be serialised, so they become capability flags and the
integration invokes them by key through `POST /api/action`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import Snapshot


def _plain(value: Any) -> Any:
    """Reduce a value to something json.dumps can handle."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _attrs(attributes: dict[str, Any] | None) -> dict[str, Any] | None:
    if not attributes:
        return None
    cleaned = {k: _plain(v) for k, v in attributes.items() if v is not None}
    return cleaned or None


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Flatten a snapshot into JSON-safe lists."""
    return {
        "devices": [
            {
                "key": d.key,
                "name": d.name,
                "manufacturer": d.manufacturer,
                "model": d.model,
                "sw_version": d.sw_version,
                "hw_version": d.hw_version,
                "serial_number": d.serial_number,
                "configuration_url": d.configuration_url,
                "via_device": d.via_device,
            }
            for d in snapshot.devices.values()
        ],
        "sensors": [
            {
                "key": r.key,
                "name": r.name,
                "value": _plain(r.value),
                "device_key": r.device_key,
                "device_class": r.device_class,
                "state_class": r.state_class,
                "unit": r.unit,
                "icon": r.icon,
                "entity_category": r.entity_category,
                "enabled_default": r.enabled_default,
                "options": r.options,
                "suggested_display_precision": r.suggested_display_precision,
                "attributes": _attrs(r.attributes),
                "severity": r.severity,
                "reason": r.reason,
            }
            for r in snapshot.sensors.values()
        ],
        "binary_sensors": [
            {
                "key": r.key,
                "name": r.name,
                "value": r.value,
                "device_key": r.device_key,
                "device_class": r.device_class,
                "icon": r.icon,
                "entity_category": r.entity_category,
                "enabled_default": r.enabled_default,
                "attributes": _attrs(r.attributes),
                "severity": r.severity,
                "reason": r.reason,
            }
            for r in snapshot.binary_sensors.values()
        ],
        "updates": [
            {
                "key": u.key,
                "name": u.name,
                "installed_version": u.installed_version,
                "latest_version": u.latest_version,
                "device_key": u.device_key,
                "title": u.title,
                "release_url": u.release_url,
                "release_summary": u.release_summary,
                "entity_category": u.entity_category,
                "enabled_default": u.enabled_default,
                # The integration renders an Install button from this.
                "can_install": u.install is not None,
                "in_progress": u.in_progress,
                "attributes": _attrs(u.attributes),
            }
            for u in snapshot.updates.values()
        ],
        "buttons": [
            {
                "key": b.key,
                "name": b.name,
                "device_key": b.device_key,
                "device_class": b.device_class,
                "icon": b.icon,
                "entity_category": b.entity_category,
                "enabled_default": b.enabled_default,
            }
            for b in snapshot.buttons.values()
        ],
        "switches": [
            {
                "key": s.key,
                "name": s.name,
                "value": s.value,
                "device_key": s.device_key,
                "device_class": s.device_class,
                "icon": s.icon,
                "entity_category": s.entity_category,
                "enabled_default": s.enabled_default,
                "assumed_state": s.assumed_state,
                "attributes": _attrs(s.attributes),
            }
            for s in snapshot.switches.values()
        ],
    }
