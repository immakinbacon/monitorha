"""Change detection: bands, hysteresis, muting and the event log cursor."""

from __future__ import annotations

from monitorha.app.const import BinarySensorDeviceClass
from monitorha.app.events import (
    KIND_PROBLEM,
    KIND_RECOVERY,
    KIND_SOURCE_UNAVAILABLE,
    KIND_THRESHOLD_CLEAR,
    KIND_THRESHOLD_CRITICAL,
    KIND_THRESHOLD_WARNING,
    KIND_UPDATE_AVAILABLE,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
    Event,
    EventLog,
    apply_overrides,
    availability_event,
    classify,
    evaluate,
)
from monitorha.app.models import BinaryReading, DeviceMeta, Reading, Snapshot, UpdateReading

WARM = {"warn_above": 70.0, "critical_above": 85.0}


def snapshot_with(*readings) -> Snapshot:
    snapshot = Snapshot()
    snapshot.add_device(DeviceMeta(key="main", name="core-router"))
    for reading in readings:
        if isinstance(reading, BinaryReading):
            snapshot.add_binary(reading)
        elif isinstance(reading, UpdateReading):
            snapshot.add_update(reading)
        else:
            snapshot.add(reading)
    return snapshot


def temp(value) -> Reading:
    return Reading(key="cpu_temp", name="CPU temperature", value=value)


def run(previous, current, overrides=None):
    return evaluate(
        previous,
        current,
        overrides or {},
        source_id="src-1",
        source_name="core-router",
    )


# -- banding -------------------------------------------------------------


def test_classify_picks_the_worst_matching_bound() -> None:
    assert classify(50, WARM) == (SEVERITY_OK, None)
    assert classify(75, WARM)[0] == SEVERITY_WARNING
    # 90 trips both bounds; critical has to win.
    assert classify(90, WARM)[0] == SEVERITY_CRITICAL


def test_classify_explains_itself() -> None:
    _, reason = classify(90, WARM)
    assert reason == "90 is above 85"


def test_classify_handles_below_bounds() -> None:
    thresholds = {"warn_below": 10.0, "critical_below": 5.0}
    assert classify(20, thresholds)[0] == SEVERITY_OK
    assert classify(7, thresholds)[0] == SEVERITY_WARNING
    assert classify(1, thresholds)[0] == SEVERITY_CRITICAL


def test_classify_ignores_non_numeric_and_bools() -> None:
    assert classify("RouterOS 7.14", WARM) == (SEVERITY_OK, None)
    # A bool is numeric in Python; treating True as 1 would band it wrongly.
    assert classify(True, {"warn_below": 10.0}) == (SEVERITY_OK, None)


# -- diffing -------------------------------------------------------------


def test_first_poll_reports_nothing() -> None:
    """Everything looks new against no previous snapshot; that is not news."""
    assert run(None, snapshot_with(temp(90))) == []


def test_threshold_crossing_fires_once() -> None:
    overrides = {"cpu_temp": {"thresholds": WARM}}
    first = snapshot_with(temp(60))
    second = snapshot_with(temp(75))

    events = run(first, second, overrides)
    assert [e.kind for e in events] == [KIND_THRESHOLD_WARNING]
    assert events[0].severity == SEVERITY_WARNING
    assert events[0].new_state == 75

    # Still warm, same band: silence. This is the hysteresis that stops a
    # value hovering on a bound from re-firing every poll.
    assert run(second, snapshot_with(temp(78)), overrides) == []


def test_escalation_and_clearing_are_reported() -> None:
    overrides = {"cpu_temp": {"thresholds": WARM}}
    warm = snapshot_with(temp(75))

    escalated = run(warm, snapshot_with(temp(95)), overrides)
    assert [e.kind for e in escalated] == [KIND_THRESHOLD_CRITICAL]

    cleared = run(warm, snapshot_with(temp(40)), overrides)
    assert [e.kind for e in cleared] == [KIND_THRESHOLD_CLEAR]
    assert cleared[0].severity == SEVERITY_OK


def test_a_reading_without_thresholds_is_never_evaluated() -> None:
    assert run(snapshot_with(temp(10)), snapshot_with(temp(9000))) == []


def test_problem_and_recovery() -> None:
    ok = BinaryReading(
        key="health",
        name="Health",
        value=False,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )
    bad = BinaryReading(
        key="health",
        name="Health",
        value=True,
        device_class=BinarySensorDeviceClass.PROBLEM,
        reason="PSU 2 failed",
    )

    raised = run(snapshot_with(ok), snapshot_with(bad))
    assert [e.kind for e in raised] == [KIND_PROBLEM]
    # The whole point: the event says what is wrong.
    assert raised[0].reason == "PSU 2 failed"
    assert raised[0].severity == SEVERITY_CRITICAL

    assert [e.kind for e in run(snapshot_with(bad), snapshot_with(ok))] == [
        KIND_RECOVERY
    ]


def test_connectivity_is_inverted_relative_to_problem() -> None:
    """For connectivity, False is the bad state, unlike a problem sensor."""
    up = BinaryReading(
        key="wan",
        name="WAN",
        value=True,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    )
    down = BinaryReading(
        key="wan",
        name="WAN",
        value=False,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    )
    assert [e.kind for e in run(snapshot_with(up), snapshot_with(down))] == [
        KIND_PROBLEM
    ]
    assert [e.kind for e in run(snapshot_with(down), snapshot_with(up))] == [
        KIND_RECOVERY
    ]


def test_unknown_state_is_not_a_change() -> None:
    """Losing a value is absence of information, not a recovery."""
    known = BinaryReading(
        key="health",
        name="Health",
        value=True,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )
    unknown = BinaryReading(
        key="health",
        name="Health",
        value=None,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )
    assert run(snapshot_with(known), snapshot_with(unknown)) == []


def test_a_newly_appearing_monitor_is_not_a_change() -> None:
    """A Proxmox guest that has just been created has nothing to compare to."""
    added = BinaryReading(
        key="guest_101",
        name="Guest 101",
        value=True,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )
    assert run(snapshot_with(), snapshot_with(added)) == []


def test_update_becoming_available_is_reported() -> None:
    before = UpdateReading(
        key="routeros", name="RouterOS", installed_version="7.14", latest_version="7.14"
    )
    after = UpdateReading(
        key="routeros", name="RouterOS", installed_version="7.14", latest_version="7.15"
    )
    events = run(snapshot_with(before), snapshot_with(after))
    assert [e.kind for e in events] == [KIND_UPDATE_AVAILABLE]
    assert events[0].new_state == "7.15"

    # Still pending on the next poll: already reported.
    assert run(snapshot_with(after), snapshot_with(after)) == []


def test_events_carry_the_device_they_belong_to() -> None:
    overrides = {"cpu_temp": {"thresholds": WARM}}
    events = run(snapshot_with(temp(60)), snapshot_with(temp(90)), overrides)
    assert events[0].device_key == "main"
    assert events[0].device_name == "core-router"
    assert events[0].source_name == "core-router"


# -- muting --------------------------------------------------------------


def test_muted_monitor_produces_no_events() -> None:
    overrides = {"cpu_temp": {"muted": True, "thresholds": WARM}}
    assert run(snapshot_with(temp(60)), snapshot_with(temp(95)), overrides) == []


def test_muting_does_not_silence_other_monitors() -> None:
    overrides = {
        "cpu_temp": {"muted": True, "thresholds": WARM},
        "board_temp": {"thresholds": WARM},
    }
    before = snapshot_with(temp(60), Reading(key="board_temp", name="Board", value=60))
    after = snapshot_with(temp(95), Reading(key="board_temp", name="Board", value=95))
    events = run(before, after, overrides)
    assert [e.entity_key for e in events] == ["board_temp"]


# -- severity stamped onto the snapshot ----------------------------------


def test_apply_overrides_stamps_severity_for_home_assistant() -> None:
    snapshot = snapshot_with(temp(95))
    apply_overrides(snapshot, {"cpu_temp": {"thresholds": WARM}})
    reading = snapshot.sensors["cpu_temp"]
    assert reading.severity == SEVERITY_CRITICAL
    assert reading.reason == "95 is above 85"


def test_apply_overrides_leaves_healthy_readings_alone() -> None:
    snapshot = snapshot_with(temp(20))
    apply_overrides(snapshot, {"cpu_temp": {"thresholds": WARM}})
    assert snapshot.sensors["cpu_temp"].severity is None


# -- the log -------------------------------------------------------------


def make_event(name: str) -> Event:
    return Event(
        kind=KIND_PROBLEM,
        source_id="src-1",
        source_name="core-router",
        entity_key=name,
        name=name,
    )


def test_log_numbers_events_and_serves_a_cursor() -> None:
    log = EventLog()
    assert log.head == 0

    log.append([make_event("a"), make_event("b")])
    assert log.head == 2
    assert [e.seq for e in log.since(0)] == [1, 2]
    # A caller that has seen everything gets nothing back.
    assert log.since(2) == []

    log.append([make_event("c")])
    assert [e.entity_key for e in log.since(2)] == ["c"]


def test_log_is_bounded_but_keeps_numbering() -> None:
    log = EventLog()
    for index in range(600):
        log.append([make_event(str(index))])
    assert log.head == 600
    # Oldest events are dropped, and asking for them does not resurrect them.
    assert len(log.since(0)) == 500
    assert log.since(0)[0].entity_key == "100"


def test_availability_event_carries_the_error() -> None:
    event = availability_event(
        source_id="src-1",
        source_name="core-router",
        available=False,
        error="Credentials rejected",
    )
    assert event.kind == KIND_SOURCE_UNAVAILABLE
    assert event.reason == "Credentials rejected"
    assert event.severity == SEVERITY_CRITICAL


def test_the_wire_shape_is_the_public_contract() -> None:
    """Both the event log UI and users' automations read these keys by name.

    Renaming or dropping one silently breaks every automation built on it, so
    the set is pinned here deliberately.
    """
    overrides = {"cpu_temp": {"thresholds": WARM}}
    events = run(snapshot_with(temp(60)), snapshot_with(temp(95)), overrides)
    payload = events[0].as_dict()

    assert set(payload) == {
        "seq",
        "kind",
        "source_id",
        "source_name",
        "entity_key",
        "name",
        "device_key",
        "device_name",
        "old_state",
        "new_state",
        "severity",
        "reason",
        "timestamp",
    }
    assert payload["kind"] == KIND_THRESHOLD_CRITICAL
    assert payload["name"] == "CPU temperature"
    assert payload["reason"] == "95 is above 85"
    # The log renders "old → new", so both have to survive serialisation.
    assert payload["old_state"] == 60
    assert payload["new_state"] == 95


def test_events_are_json_serialisable() -> None:
    """They cross the wire and land on the Home Assistant bus verbatim."""
    import json

    log = EventLog()
    log.append([make_event("a")])
    assert json.loads(json.dumps([e.as_dict() for e in log.since(0)]))[0]["seq"] == 1
