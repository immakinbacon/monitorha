"""Per-line overrides: storage, validation, and staying out of the poller."""

from __future__ import annotations

from pathlib import Path

import pytest

from monitorha.app.store import ConfigError, Store, validate_override

MIKROTIK_SOURCE = {
    "type": "mikrotik",
    "name": "core-router",
    "host": "192.0.2.10",
    "username": "monitor",
    "password": "secret",
}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "config.json")


@pytest.fixture
def source_id(store: Store) -> str:
    return store.add(MIKROTIK_SOURCE)["id"]


# -- validation ----------------------------------------------------------


def test_blank_boxes_clear_a_bound_rather_than_setting_zero() -> None:
    result = validate_override(
        {"thresholds": {"warn_above": "70", "critical_above": "", "warn_below": None}}
    )
    assert result["thresholds"] == {"warn_above": 70.0}


def test_thresholds_must_be_numbers() -> None:
    with pytest.raises(ConfigError):
        validate_override({"thresholds": {"warn_above": "hot"}})


def test_warn_must_not_sit_beyond_critical() -> None:
    with pytest.raises(ConfigError):
        validate_override({"thresholds": {"warn_above": 90, "critical_above": 70}})
    with pytest.raises(ConfigError):
        validate_override({"thresholds": {"warn_below": 5, "critical_below": 10}})


def test_matching_bounds_are_allowed() -> None:
    """Warn and critical at the same figure is odd but not wrong."""
    assert validate_override({"thresholds": {"warn_above": 70, "critical_above": 70}})


# -- storage -------------------------------------------------------------


def test_override_round_trips_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    store = Store(path)
    src = store.add(MIKROTIK_SOURCE)["id"]
    store.set_override(src, "cpu_temp", {"muted": True, "thresholds": {"warn_above": 70}})

    reloaded = Store(path)
    assert reloaded.overrides_for(src)["cpu_temp"] == {
        "muted": True,
        "thresholds": {"warn_above": 70.0},
    }


def test_a_default_override_is_stored_as_nothing(store: Store, source_id: str) -> None:
    """Otherwise the file grows an entry per monitor that was merely opened."""
    store.set_override(source_id, "cpu_temp", {"muted": True})
    assert "cpu_temp" in store.overrides_for(source_id)

    store.set_override(source_id, "cpu_temp", {"muted": False, "thresholds": {}})
    assert store.overrides_for(source_id) == {}


def test_clearing_an_unknown_override_is_harmless(store: Store, source_id: str) -> None:
    store.clear_override(source_id, "never-set")
    assert store.overrides_for(source_id) == {}


def test_overrides_need_a_real_source(store: Store) -> None:
    with pytest.raises(ConfigError):
        store.set_override("no-such-source", "cpu_temp", {"muted": True})


def test_removing_a_source_drops_its_overrides(store: Store, source_id: str) -> None:
    """A re-added device must not inherit the old one's mutes."""
    store.set_override(source_id, "cpu_temp", {"muted": True})
    store.remove(source_id)
    assert store.overrides_for(source_id) == {}


def test_overrides_are_per_source(store: Store) -> None:
    first = store.add(MIKROTIK_SOURCE)["id"]
    second = store.add({**MIKROTIK_SOURCE, "host": "10.0.0.2"})["id"]
    store.set_override(first, "cpu_temp", {"muted": True})
    assert store.overrides_for(second) == {}


# -- the reason they live outside the source config ----------------------


def test_setting_an_override_does_not_touch_the_source_config(
    store: Store, source_id: str
) -> None:
    """`Manager.sync` rebuilds a runner when its config changes.

    Overrides therefore have to sit outside the source dict, or nudging a
    threshold would restart the poller — re-authenticating and throwing away
    the current snapshot every time.
    """
    before = store.sources
    store.set_override(source_id, "cpu_temp", {"thresholds": {"warn_above": 70}})
    assert store.sources == before

    store.clear_override(source_id, "cpu_temp")
    assert store.sources == before
