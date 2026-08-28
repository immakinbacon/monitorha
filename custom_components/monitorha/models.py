"""Parsing the add-on's snapshot into Home-Assistant-typed data.

The add-on emits the *values* of Home Assistant's enums as plain strings. Here
they are converted back into the real enums, tolerantly: an add-on newer than
the integration may send a device class this Home Assistant does not know, and
that must degrade to "no device class" rather than breaking the whole poll.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.button import ButtonDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

_EnumT = TypeVar("_EnumT")


def _enum(cls: type[_EnumT], value: Any) -> _EnumT | None:
    """Convert a wire string into an enum member, or None if unrecognised."""
    if not value:
        return None
    try:
        return cls(value)
    except ValueError:
        return None


@dataclass(slots=True)
class DeviceMeta:
    key: str
    name: str
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None
    hw_version: str | None = None
    serial_number: str | None = None
    configuration_url: str | None = None
    via_device: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DeviceMeta:
        return cls(
            key=data["key"],
            name=data["name"],
            manufacturer=data.get("manufacturer"),
            model=data.get("model"),
            sw_version=data.get("sw_version"),
            hw_version=data.get("hw_version"),
            serial_number=data.get("serial_number"),
            configuration_url=data.get("configuration_url"),
            via_device=data.get("via_device"),
        )


@dataclass(slots=True)
class Reading:
    key: str
    name: str
    value: Any
    device_key: str
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    unit: str | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True
    options: list[str] | None = None
    suggested_display_precision: int | None = None
    attributes: dict[str, Any] | None = None
    severity: str | None = None
    reason: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Reading:
        device_class = _enum(SensorDeviceClass, data.get("device_class"))
        value = data.get("value")
        if device_class is SensorDeviceClass.TIMESTAMP and isinstance(value, str):
            # Timestamps cross the wire as ISO strings.
            value = dt_util.parse_datetime(value)
        return cls(
            key=data["key"],
            name=data["name"],
            value=value,
            device_key=data.get("device_key", "main"),
            device_class=device_class,
            state_class=_enum(SensorStateClass, data.get("state_class")),
            unit=data.get("unit"),
            icon=data.get("icon"),
            entity_category=_enum(EntityCategory, data.get("entity_category")),
            enabled_default=data.get("enabled_default", True),
            options=data.get("options"),
            suggested_display_precision=data.get("suggested_display_precision"),
            attributes=data.get("attributes"),
            severity=data.get("severity"),
            reason=data.get("reason"),
        )


@dataclass(slots=True)
class BinaryReading:
    key: str
    name: str
    value: bool | None
    device_key: str
    device_class: BinarySensorDeviceClass | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True
    attributes: dict[str, Any] | None = None
    severity: str | None = None
    reason: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> BinaryReading:
        return cls(
            key=data["key"],
            name=data["name"],
            value=data.get("value"),
            device_key=data.get("device_key", "main"),
            device_class=_enum(BinarySensorDeviceClass, data.get("device_class")),
            icon=data.get("icon"),
            entity_category=_enum(EntityCategory, data.get("entity_category")),
            enabled_default=data.get("enabled_default", True),
            attributes=data.get("attributes"),
            severity=data.get("severity"),
            reason=data.get("reason"),
        )


@dataclass(slots=True)
class UpdateReading:
    key: str
    name: str
    installed_version: str | None
    latest_version: str | None
    device_key: str
    title: str | None = None
    release_url: str | None = None
    release_summary: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True
    can_install: bool = False
    in_progress: bool = False
    attributes: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> UpdateReading:
        return cls(
            key=data["key"],
            name=data["name"],
            installed_version=data.get("installed_version"),
            latest_version=data.get("latest_version"),
            device_key=data.get("device_key", "main"),
            title=data.get("title"),
            release_url=data.get("release_url"),
            release_summary=data.get("release_summary"),
            entity_category=_enum(EntityCategory, data.get("entity_category")),
            enabled_default=data.get("enabled_default", True),
            can_install=data.get("can_install", False),
            in_progress=data.get("in_progress", False),
            attributes=data.get("attributes"),
        )


@dataclass(slots=True)
class ButtonSpec:
    key: str
    name: str
    device_key: str
    device_class: ButtonDeviceClass | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ButtonSpec:
        return cls(
            key=data["key"],
            name=data["name"],
            device_key=data.get("device_key", "main"),
            device_class=_enum(ButtonDeviceClass, data.get("device_class")),
            icon=data.get("icon"),
            entity_category=_enum(EntityCategory, data.get("entity_category")),
            enabled_default=data.get("enabled_default", True),
        )


@dataclass(slots=True)
class SwitchSpec:
    key: str
    name: str
    value: bool | None
    device_key: str
    device_class: SwitchDeviceClass | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True
    assumed_state: bool = False
    attributes: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SwitchSpec:
        return cls(
            key=data["key"],
            name=data["name"],
            value=data.get("value"),
            device_key=data.get("device_key", "main"),
            device_class=_enum(SwitchDeviceClass, data.get("device_class")),
            icon=data.get("icon"),
            entity_category=_enum(EntityCategory, data.get("entity_category")),
            enabled_default=data.get("enabled_default", True),
            assumed_state=data.get("assumed_state", False),
            attributes=data.get("attributes"),
        )


@dataclass(slots=True)
class SourceData:
    """One monitored device or cluster, as reported by the add-on."""

    id: str
    type: str
    name: str
    available: bool
    error: str | None
    last_update: datetime | None
    devices: dict[str, DeviceMeta] = field(default_factory=dict)
    sensors: dict[str, Reading] = field(default_factory=dict)
    binary_sensors: dict[str, BinaryReading] = field(default_factory=dict)
    updates: dict[str, UpdateReading] = field(default_factory=dict)
    buttons: dict[str, ButtonSpec] = field(default_factory=dict)
    switches: dict[str, SwitchSpec] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SourceData:
        last_update = data.get("last_update")
        return cls(
            id=data["id"],
            type=data.get("type", "unknown"),
            name=data.get("name", data["id"]),
            available=data.get("available", False),
            error=data.get("error"),
            last_update=dt_util.parse_datetime(last_update) if last_update else None,
            devices={
                d["key"]: DeviceMeta.from_json(d) for d in data.get("devices") or []
            },
            sensors={
                r["key"]: Reading.from_json(r) for r in data.get("sensors") or []
            },
            binary_sensors={
                r["key"]: BinaryReading.from_json(r)
                for r in data.get("binary_sensors") or []
            },
            updates={
                u["key"]: UpdateReading.from_json(u) for u in data.get("updates") or []
            },
            buttons={
                b["key"]: ButtonSpec.from_json(b) for b in data.get("buttons") or []
            },
            switches={
                s["key"]: SwitchSpec.from_json(s) for s in data.get("switches") or []
            },
        )


@dataclass(slots=True)
class AddonData:
    """Everything the add-on is currently reporting."""

    sources: dict[str, SourceData] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AddonData:
        return cls(
            sources={
                s["id"]: SourceData.from_json(s) for s in data.get("sources") or []
            }
        )

    def iter_items(self, collection: str):
        """Yield ((source, key), item) across every source.

        Entity keys are only unique within a source, so platforms address
        entities by the source/key pair.
        """
        for source in self.sources.values():
            for key, item in getattr(source, collection).items():
                yield (source.id, key), item
