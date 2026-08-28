"""Normalised data model shared by every device backend.

Identical in shape to the model the integration reconstructs on the Home
Assistant side; `serialize.py` is the bridge between the two.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .const import (
    MAIN,
    BinarySensorDeviceClass,
    ButtonDeviceClass,
    EntityCategory,
    SensorDeviceClass,
    SensorStateClass,
    SwitchDeviceClass,
)


@dataclass(slots=True)
class DeviceMeta:
    """A device to register in the Home Assistant device registry."""

    key: str
    name: str
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None
    hw_version: str | None = None
    serial_number: str | None = None
    configuration_url: str | None = None
    via_device: str | None = None
    """Key of the parent device, if this device hangs off another one."""


@dataclass(slots=True)
class Reading:
    """A numeric or textual sensor value."""

    key: str
    name: str
    value: Any
    device_key: str = MAIN
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
    """Threshold band this value currently sits in; set by the event engine."""
    reason: str | None = None
    """Human-readable explanation of a non-OK severity."""


@dataclass(slots=True)
class BinaryReading:
    """An on/off or problem/ok state."""

    key: str
    name: str
    value: bool | None
    device_key: str = MAIN
    device_class: BinarySensorDeviceClass | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True
    attributes: dict[str, Any] | None = None
    severity: str | None = None
    reason: str | None = None
    """Why this reads as a problem — a bare `problem` state explains nothing."""


@dataclass(slots=True)
class UpdateReading:
    """An installed-vs-available version pair."""

    key: str
    name: str
    installed_version: str | None
    latest_version: str | None
    device_key: str = MAIN
    title: str | None = None
    release_url: str | None = None
    release_summary: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True
    install: Callable[[], Awaitable[None]] | None = None
    """Set when the backend can actually perform the upgrade."""
    in_progress: bool = False
    attributes: dict[str, Any] | None = None


def is_pending(update: UpdateReading) -> bool:
    """Whether an update reading describes an upgrade that is actually waiting.

    A backend that can only read a version back publishes it with `latest`
    equal to `installed`, so this is what separates "here is the firmware
    version" from "there is something newer to install".
    """
    return (
        update.latest_version is not None
        and update.installed_version != update.latest_version
    )


@dataclass(slots=True)
class ButtonSpec:
    """A one-shot action."""

    key: str
    name: str
    press: Callable[[], Awaitable[None]]
    device_key: str = MAIN
    device_class: ButtonDeviceClass | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True


@dataclass(slots=True)
class SwitchSpec:
    """A two-state control, such as chassis power or a PoE-out port."""

    key: str
    name: str
    value: bool | None
    turn: Callable[[bool], Awaitable[None]]
    device_key: str = MAIN
    device_class: SwitchDeviceClass | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True
    assumed_state: bool = False
    attributes: dict[str, Any] | None = None


@dataclass(slots=True)
class Snapshot:
    """One complete poll of a device."""

    devices: dict[str, DeviceMeta] = field(default_factory=dict)
    sensors: dict[str, Reading] = field(default_factory=dict)
    binary_sensors: dict[str, BinaryReading] = field(default_factory=dict)
    updates: dict[str, UpdateReading] = field(default_factory=dict)
    buttons: dict[str, ButtonSpec] = field(default_factory=dict)
    switches: dict[str, SwitchSpec] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    """Backend facts about the source itself rather than about an entity.

    `cluster_id` is the one that matters: it lets the manager recognise that
    two configured hosts are reporting the same cluster.
    """

    def add_device(self, device: DeviceMeta) -> None:
        self.devices[device.key] = device

    def add(self, reading: Reading) -> None:
        self.sensors[reading.key] = reading

    def add_binary(self, reading: BinaryReading) -> None:
        self.binary_sensors[reading.key] = reading

    def add_update(self, reading: UpdateReading) -> None:
        self.updates[reading.key] = reading

    def add_button(self, spec: ButtonSpec) -> None:
        self.buttons[spec.key] = spec

    def add_switch(self, spec: SwitchSpec) -> None:
        self.switches[spec.key] = spec

    def pending_updates(self) -> list[UpdateReading]:
        """Every update in this snapshot with a newer version available."""
        return [u for u in self.updates.values() if is_pending(u)]
