"""Change detection between two polls.

The pollers are otherwise stateless: each cycle replaces the last snapshot and
nothing looks at what moved. This module is the missing comparison. It turns a
pair of snapshots into a list of `Event`s, which the integration republishes on
the Home Assistant bus so automations can trigger on them.

Two rules keep the stream quiet enough to be useful:

* A **muted** line produces nothing at all.
* A threshold only fires when the value crosses into a *different band*. A
  figure hovering either side of a bound therefore reports once, not on every
  poll.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import BinaryReading, Reading, Snapshot, is_pending
from .store import THRESHOLD_FIELDS

# Bands a numeric reading can sit in, worst last. Ordering is what lets the
# engine tell an escalation from a recovery without special-casing each pair.
SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITY_ORDER = (SEVERITY_OK, SEVERITY_WARNING, SEVERITY_CRITICAL)

# Event kinds. These are the values an automation matches on, so they are part
# of the add-on's public interface and should not be renamed lightly.
KIND_PROBLEM = "problem"
KIND_RECOVERY = "recovery"
KIND_THRESHOLD_WARNING = "threshold_warning"
KIND_THRESHOLD_CRITICAL = "threshold_critical"
KIND_THRESHOLD_CLEAR = "threshold_clear"
KIND_STATE_CHANGE = "state_change"
KIND_UPDATE_AVAILABLE = "update_available"
KIND_SOURCE_AVAILABLE = "source_available"
KIND_SOURCE_UNAVAILABLE = "source_unavailable"

# How many events to keep. The integration polls every 30s by default, so this
# is a generous margin against a slow or briefly disconnected consumer.
LOG_SIZE = 500


@dataclass(slots=True)
class Event:
    """One observed change, in the shape the HA event bus will carry."""

    kind: str
    source_id: str
    source_name: str
    entity_key: str
    name: str
    device_key: str = ""
    device_name: str = ""
    old_state: Any = None
    new_state: Any = None
    severity: str = SEVERITY_OK
    reason: str | None = None
    timestamp: str = ""
    seq: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventLog:
    """Bounded, monotonically numbered event history.

    Consumers poll with the last `seq` they saw, so a restart of either side
    resynchronises without replaying the whole buffer.
    """

    _events: deque[Event] = field(default_factory=lambda: deque(maxlen=LOG_SIZE))
    _seq: int = 0

    @property
    def head(self) -> int:
        """Sequence number of the newest event; 0 when nothing has happened."""
        return self._seq

    def append(self, events: list[Event]) -> None:
        for event in events:
            self._seq += 1
            event.seq = self._seq
            self._events.append(event)

    def since(self, seq: int) -> list[Event]:
        """Every event newer than `seq`, oldest first."""
        return [e for e in self._events if e.seq > seq]


def _to_float(value: Any) -> float | None:
    """Numeric value of a reading, or None if it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify(value: Any, thresholds: dict[str, float]) -> tuple[str, str | None]:
    """Band a numeric value falls into, with the bound that put it there.

    The worst matching bound wins, so a value below `critical_below` reports as
    critical even when it also trips the warning bound.
    """
    number = _to_float(value)
    if number is None or not thresholds:
        return SEVERITY_OK, None

    for name, severity in (
        ("critical_above", SEVERITY_CRITICAL),
        ("critical_below", SEVERITY_CRITICAL),
        ("warn_above", SEVERITY_WARNING),
        ("warn_below", SEVERITY_WARNING),
    ):
        bound = thresholds.get(name)
        if bound is None:
            continue
        if name.endswith("_above") and number > bound:
            return severity, f"{number:g} is above {bound:g}"
        if name.endswith("_below") and number < bound:
            return severity, f"{number:g} is below {bound:g}"
    return SEVERITY_OK, None


def _threshold_kind(severity: str) -> str:
    if severity == SEVERITY_CRITICAL:
        return KIND_THRESHOLD_CRITICAL
    if severity == SEVERITY_WARNING:
        return KIND_THRESHOLD_WARNING
    return KIND_THRESHOLD_CLEAR


def thresholds_for(overrides: dict[str, Any], key: str) -> dict[str, float]:
    entry = overrides.get(key) or {}
    raw = entry.get("thresholds") or {}
    return {k: v for k, v in raw.items() if k in THRESHOLD_FIELDS}


def is_muted(overrides: dict[str, Any], key: str) -> bool:
    return bool((overrides.get(key) or {}).get("muted"))


def apply_overrides(snapshot: Snapshot, overrides: dict[str, Any]) -> None:
    """Stamp each reading with its current band, in place.

    Done before serialisation so the severity and its explanation reach Home
    Assistant as attributes, not only the event stream.
    """
    for reading in snapshot.sensors.values():
        severity, reason = classify(
            reading.value, thresholds_for(overrides, reading.key)
        )
        if severity != SEVERITY_OK:
            reading.severity = severity
            reading.reason = reason


def _device_name(snapshot: Snapshot, device_key: str) -> str:
    device = snapshot.devices.get(device_key)
    return device.name if device else device_key


def _readings(snapshot: Snapshot | None) -> dict[str, Reading]:
    return snapshot.sensors if snapshot else {}


def _binaries(snapshot: Snapshot | None) -> dict[str, BinaryReading]:
    return snapshot.binary_sensors if snapshot else {}


def evaluate(
    previous: Snapshot | None,
    current: Snapshot,
    overrides: dict[str, Any],
    *,
    source_id: str,
    source_name: str,
) -> list[Event]:
    """Compare two polls and describe what changed.

    The first poll after a start produces nothing: with no previous snapshot
    every line would look like a fresh change and bury the real ones.
    """
    if previous is None:
        return []

    now = datetime.now(UTC).isoformat()
    events: list[Event] = []

    def emit(
        kind: str,
        key: str,
        name: str,
        device_key: str,
        old: Any,
        new: Any,
        severity: str = SEVERITY_OK,
        reason: str | None = None,
    ) -> None:
        events.append(
            Event(
                kind=kind,
                source_id=source_id,
                source_name=source_name,
                entity_key=key,
                name=name,
                device_key=device_key,
                device_name=_device_name(current, device_key),
                old_state=old,
                new_state=new,
                severity=severity,
                reason=reason,
                timestamp=now,
            )
        )

    old_binaries = _binaries(previous)
    for key, reading in _binaries(current).items():
        if is_muted(overrides, key):
            continue
        before = old_binaries.get(key)
        # A line that has only just appeared has nothing to be compared with,
        # and an unknown value is absence of information, not a change.
        if before is None or before.value == reading.value or reading.value is None:
            continue

        if reading.device_class == "problem":
            kind = KIND_PROBLEM if reading.value else KIND_RECOVERY
            severity = SEVERITY_CRITICAL if reading.value else SEVERITY_OK
        elif reading.device_class == "connectivity":
            # Connectivity is inverted relative to problem: False is the bad one.
            kind = KIND_RECOVERY if reading.value else KIND_PROBLEM
            severity = SEVERITY_OK if reading.value else SEVERITY_CRITICAL
        else:
            kind = KIND_STATE_CHANGE
            severity = SEVERITY_OK
        emit(
            kind,
            key,
            reading.name,
            reading.device_key,
            before.value,
            reading.value,
            severity,
            reading.reason,
        )

    old_readings = _readings(previous)
    for key, reading in _readings(current).items():
        if is_muted(overrides, key):
            continue
        thresholds = thresholds_for(overrides, key)
        if not thresholds:
            continue
        before = old_readings.get(key)
        if before is None:
            continue
        old_band, _ = classify(before.value, thresholds)
        new_band, reason = classify(reading.value, thresholds)
        # The hysteresis that keeps a value sitting on a bound from flapping:
        # only a change of band is worth reporting.
        if old_band == new_band:
            continue
        emit(
            _threshold_kind(new_band),
            key,
            reading.name,
            reading.device_key,
            before.value,
            reading.value,
            new_band,
            reason,
        )

    old_updates = previous.updates
    for key, update in current.updates.items():
        if is_muted(overrides, key):
            continue
        before = old_updates.get(key)
        if before is None:
            continue
        pending = is_pending(update)
        was_pending = is_pending(before)
        if pending and not was_pending:
            emit(
                KIND_UPDATE_AVAILABLE,
                key,
                update.name,
                update.device_key,
                before.installed_version,
                update.latest_version,
                SEVERITY_WARNING,
                f"{update.name} {update.latest_version} is available",
            )

    return events


def availability_event(
    *,
    source_id: str,
    source_name: str,
    available: bool,
    error: str | None,
) -> Event:
    """The source itself going up or down, which no snapshot can describe."""
    return Event(
        kind=KIND_SOURCE_AVAILABLE if available else KIND_SOURCE_UNAVAILABLE,
        source_id=source_id,
        source_name=source_name,
        entity_key="source",
        name=source_name,
        old_state=not available,
        new_state=available,
        severity=SEVERITY_OK if available else SEVERITY_CRITICAL,
        reason=None if available else error,
        timestamp=datetime.now(UTC).isoformat(),
    )
