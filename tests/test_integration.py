"""End-to-end tests for the integration.

Boots a real Home Assistant, points the integration at a stub of the add-on's
HTTP API, and checks that the snapshot becomes correct native entities.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from custom_components.monitorha.const import CONF_TOKEN, DOMAIN

from .test_mikrotik import ROUTES as MIKROTIK_ROUTES
from .test_proxmox import ROUTES as PROXMOX_ROUTES
from .test_redfish import RESET_TARGET
from .test_redfish import ROUTES as REDFISH_ROUTES
from .test_swos import LITE_ROUTES as SWOS_ROUTES

ENTRY_DATA = {CONF_HOST: "local-monitorha", CONF_PORT: 8099, CONF_TOKEN: "s3cret"}


class FakeAddon:
    """Stands in for the add-on's HTTP API.

    Builds its snapshot by running the real backends against canned device
    payloads, so what the integration parses is exactly what the add-on emits.
    """

    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self.sources = sources
        self.actions: list[dict[str, Any]] = []
        self.fail: Exception | None = None
        self.snapshot: dict[str, Any] = {"sources": []}
        # The change-event log the coordinator polls alongside the snapshot.
        self.events: list[dict[str, Any]] = []
        self.head = 0

    async def build(self, make_session) -> None:
        from monitorha.app.manager import SourceRunner
        from monitorha.app.store import validate_source

        payloads = []
        for spec in self.sources:
            config = validate_source(spec["config"])
            config["id"] = spec["id"]
            runner = SourceRunner(config)
            await runner._session.close()
            runner._session = make_session(spec["routes"])
            from monitorha.app.api import build_client

            runner._client = build_client(config, runner._session)
            await runner._poll_once()
            payloads.append(runner.as_dict())
        self.snapshot = {"sources": payloads}

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise NotImplementedError


def patched_client(addon: FakeAddon):
    """Replace the integration's HTTP calls with the fake add-on."""

    async def _request(self, method: str, path: str, payload: Any = None) -> Any:
        if addon.fail is not None:
            raise addon.fail
        if path == "/api/snapshot":
            return addon.snapshot
        if path == "/api/action":
            addon.actions.append(payload)
            return {"ok": True}
        if path == "/api/health":
            return {"ok": True}
        if path.startswith("/api/events"):
            since = int(path.partition("since=")[2] or 0)
            return {
                "head": addon.head,
                "events": [e for e in addon.events if e["seq"] > since],
            }
        raise AssertionError(f"Unexpected add-on call: {path}")

    return patch(
        "custom_components.monitorha.coordinator.AddonClient._request", _request
    )


@pytest.fixture
async def addon(make_session) -> FakeAddon:
    fake = FakeAddon(
        [
            {
                "id": "src-mt",
                "routes": MIKROTIK_ROUTES,
                "config": {
                    "type": "mikrotik",
                    "name": "core-router",
                    "host": "192.0.2.10",
                    "username": "monitor",
                    "password": "secret",
                },
            },
            {
                "id": "src-pve",
                "routes": PROXMOX_ROUTES,
                "config": {
                    "type": "proxmox",
                    "name": "homelab",
                    "scope": "cluster",
                    "host": "192.0.2.100",
                    "token_id": "monitoring@pve!ha",
                    "token_secret": "x",
                },
            },
            {
                "id": "src-sw",
                "routes": SWOS_ROUTES,
                "config": {
                    "type": "swos",
                    "name": "switch1.example",
                    "host": "10.0.3.234",
                    "port": 80,
                    "username": "admin",
                    "password": "secret",
                    "check_firmware": False,
                },
            },
            {
                "id": "src-bmc",
                "routes": REDFISH_ROUTES,
                "config": {
                    "type": "redfish",
                    "name": "vm-host-01",
                    "host": "10.0.0.20",
                    "username": "ADMIN",
                    "password": "secret",
                },
            },
        ]
    )
    await fake.build(make_session)
    return fake


async def setup_entry(hass: HomeAssistant, addon: FakeAddon) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    with patched_client(addon):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


# -- config flow ---------------------------------------------------------


async def test_manual_config_flow(hass: HomeAssistant, addon: FakeAddon) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patched_client(addon):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ENTRY_DATA
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == "s3cret"


async def test_hassio_discovery_flow(hass: HomeAssistant, addon: FakeAddon) -> None:
    """The add-on announces itself; the user only confirms."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={"host": "local-monitorha", "port": 8099, "token": "s3cret"},
            name="Infrastructure Monitor",
            slug="monitorha",
            uuid="abc123",
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"

    with patched_client(addon):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "local-monitorha"


async def test_single_instance_only(hass: HomeAssistant, addon: FakeAddon) -> None:
    await setup_entry(hass, addon)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT


async def test_bad_token_reports_invalid_auth(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    from custom_components.monitorha.coordinator import AddonAuthError

    addon.fail = AddonAuthError("nope")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patched_client(addon):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ENTRY_DATA
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


# -- entities ------------------------------------------------------------


async def test_entities_from_every_source(hass: HomeAssistant, addon: FakeAddon) -> None:
    entry = await setup_entry(hass, addon)
    assert entry.state is ConfigEntryState.LOADED

    # MikroTik
    assert hass.states.get("sensor.core_router_cpu_load").state == "7.0"
    assert hass.states.get("binary_sensor.core_router_psu2_state").state == "on"
    assert hass.states.get("switch.core_router_ether3_poe_out").state == "on"

    # Proxmox
    assert hass.states.get("sensor.pve1_cpu_used").state == "12.34"
    assert hass.states.get("binary_sensor.pve1_zfs_tank_health").state == "on"
    assert hass.states.get("switch.docker_host_101_power").state == "on"

    # Redfish
    assert hass.states.get("sensor.vm_host_01_cpu1_temp").state == "47.0"
    assert hass.states.get("switch.vm_host_01_power").state == "on"


async def test_timestamps_survive_the_wire(hass: HomeAssistant, addon: FakeAddon) -> None:
    """Timestamps are ISO strings in JSON and must come back as datetimes."""
    await setup_entry(hass, addon)
    state = hass.states.get("sensor.core_router_last_boot")
    assert state.attributes["device_class"] == "timestamp"
    # A parsed datetime renders as an ISO string, not the word "unknown".
    assert state.state not in ("unknown", "unavailable")
    assert state.state.startswith("20")


async def test_update_entity_install_capability(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    await setup_entry(hass, addon)
    routeros = hass.states.get("update.core_router_routeros")
    assert routeros.state == "on"
    assert routeros.attributes["installed_version"] == "7.14.3"
    # RouterOS can be upgraded through its API.
    assert routeros.attributes["supported_features"] == 1

    # Proxmox exposes no upgrade endpoint, so no INSTALL feature.
    pve = hass.states.get("update.pve1_proxmox_ve")
    assert pve.attributes["supported_features"] == 0


async def test_device_hierarchy_is_preserved(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    from homeassistant.helpers import device_registry as dr

    entry = await setup_entry(hass, addon)
    registry = dr.async_get(hass)

    cluster = registry.async_get_device({(DOMAIN, f"{entry.entry_id}:src-pve:main")})
    node = registry.async_get_device(
        {(DOMAIN, f"{entry.entry_id}:src-pve:node_pve1")}
    )
    guest = registry.async_get_device(
        {(DOMAIN, f"{entry.entry_id}:src-pve:guest_101")}
    )
    assert node.via_device_id == cluster.id
    assert guest.via_device_id == node.id


async def test_sources_do_not_collide(hass: HomeAssistant, addon: FakeAddon) -> None:
    """Two sources can use the same entity key without clashing."""
    entry = await setup_entry(hass, addon)
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert f"{entry.entry_id}_src-mt_last_boot" in unique_ids
    assert f"{entry.entry_id}_src-bmc_power_state" in unique_ids
    # Every entity got a distinct id.
    assert len(unique_ids) == len(
        er.async_entries_for_config_entry(registry, entry.entry_id)
    )


# -- actions -------------------------------------------------------------


async def test_switch_calls_the_addon(hass: HomeAssistant, addon: FakeAddon) -> None:
    await setup_entry(hass, addon)
    with patched_client(addon):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": "switch.vm_host_01_power"},
            blocking=True,
        )
    assert addon.actions[-1] == {
        "source_id": "src-bmc",
        "kind": "switch",
        "key": "power",
        "value": False,
    }


async def test_button_calls_the_addon(hass: HomeAssistant, addon: FakeAddon) -> None:
    await setup_entry(hass, addon)
    with patched_client(addon):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.vm_host_01_power_on"},
            blocking=True,
        )
    assert {
        "source_id": "src-bmc",
        "kind": "button",
        "key": "power_on",
        "value": None,
    } in addon.actions
    # The action name maps to the real Redfish reset target on the add-on side.
    assert RESET_TARGET.endswith("ComputerSystem.Reset")


async def test_switch_is_optimistic_until_confirmed(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    """Power state lags the command, so the UI must not snap back."""
    await setup_entry(hass, addon)
    with patched_client(addon):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": "switch.vm_host_01_power"},
            blocking=True,
        )
        await hass.async_block_till_done()
    # The stub add-on still reports "On", but the entity shows the request.
    assert hass.states.get("switch.vm_host_01_power").state == "off"


# -- resilience ----------------------------------------------------------


async def test_unavailable_source_marks_entities_unavailable(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    entry = await setup_entry(hass, addon)
    assert hass.states.get("sensor.core_router_cpu_load").state == "7.0"

    for source in addon.snapshot["sources"]:
        if source["id"] == "src-mt":
            source["available"] = False
            source["error"] = "Connection refused"

    with patched_client(addon):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get("sensor.core_router_cpu_load").state == "unavailable"
    # Other sources are unaffected.
    assert hass.states.get("sensor.vm_host_01_cpu1_temp").state == "47.0"


async def test_unknown_enum_values_do_not_break_parsing(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    """An add-on newer than the integration must degrade, not crash."""
    for source in addon.snapshot["sources"]:
        for sensor in source["sensors"]:
            sensor["device_class"] = "something_from_the_future"

    entry = await setup_entry(hass, addon)
    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get("sensor.core_router_cpu_load")
    assert state.state == "7.0"
    assert "device_class" not in state.attributes


async def test_new_source_appears_without_reload(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    """Adding a device in the add-on UI must surface without touching HA."""
    entry = await setup_entry(hass, addon)
    assert hass.states.get("sensor.core_router_cpu_load") is not None

    extra = addon.snapshot["sources"][0]
    addon.snapshot["sources"] = [
        {**extra, "id": "src-new", "name": "second-router"},
        *addon.snapshot["sources"],
    ]
    with patched_client(addon):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_src-new_cpu_load"
    )


async def test_unload(hass: HomeAssistant, addon: FakeAddon) -> None:
    entry = await setup_entry(hass, addon)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


# -- change events on the Home Assistant bus -----------------------------


async def test_events_reach_the_bus(hass: HomeAssistant, addon: FakeAddon) -> None:
    """What an automation triggers on."""
    from custom_components.monitorha.const import EVENT_MONITOR

    entry = await setup_entry(hass, addon)
    fired: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_MONITOR, lambda event: fired.append(event.data))

    addon.events = [
        {
            "seq": 1,
            "kind": "threshold_critical",
            "source_id": "src-bmc",
            "source_name": "vm-host-01",
            "entity_key": "cpu_temp",
            "name": "CPU temperature",
            "reason": "95 is above 85",
            "severity": "critical",
        }
    ]
    addon.head = 1

    with patched_client(addon):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert len(fired) == 1
    assert fired[0]["kind"] == "threshold_critical"
    assert fired[0]["reason"] == "95 is above 85"


async def test_history_is_not_replayed_on_startup(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    """A restart must not re-fire automations for changes already handled."""
    from custom_components.monitorha.const import EVENT_MONITOR

    addon.events = [
        {"seq": 1, "kind": "problem", "source_id": "src-bmc", "entity_key": "old"},
        {"seq": 2, "kind": "problem", "source_id": "src-bmc", "entity_key": "older"},
    ]
    addon.head = 2

    fired: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_MONITOR, lambda event: fired.append(event.data))
    entry = await setup_entry(hass, addon)
    await hass.async_block_till_done()

    # The backlog that existed before this config entry started is adopted,
    # not replayed.
    assert fired == []

    addon.events.append(
        {"seq": 3, "kind": "recovery", "source_id": "src-bmc", "entity_key": "new"}
    )
    addon.head = 3
    with patched_client(addon):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert [e["entity_key"] for e in fired] == ["new"]


async def test_event_failure_does_not_break_the_poll(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    """Entities are still good even if the event log cannot be read."""
    from custom_components.monitorha.coordinator import AddonConnectionError

    entry = await setup_entry(hass, addon)

    async def _request(self, method: str, path: str, payload: Any = None) -> Any:
        if path.startswith("/api/events"):
            raise AddonConnectionError("boom")
        return addon.snapshot

    with patch(
        "custom_components.monitorha.coordinator.AddonClient._request", _request
    ):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert entry.runtime_data.last_update_success


async def test_severity_and_reason_become_attributes(
    hass: HomeAssistant, addon: FakeAddon
) -> None:
    """A 'problem' state that does not say what is wrong is not much use."""
    for source in addon.snapshot["sources"]:
        for binary in source["binary_sensors"]:
            binary["reason"] = "PSU 2 failed"
            binary["severity"] = "critical"
            break

    await setup_entry(hass, addon)
    states = [
        s
        for s in hass.states.async_all("binary_sensor")
        if s.attributes.get("reason") == "PSU 2 failed"
    ]
    assert states
    assert states[0].attributes["severity"] == "critical"


async def test_swos_switch_entities(hass: HomeAssistant, addon: FakeAddon) -> None:
    """A SwOS switch's readings survive the wire into real HA entities."""
    await setup_entry(hass, addon)

    assert hass.states.get("sensor.switch1_example_cpu_temperature").state == "60"
    assert (
        hass.states.get("binary_sensor.switch1_example_port4_camera_link").state == "off"
    )

    # Enum sensors have to arrive with their option list or HA rejects the state.
    speed = hass.states.get("sensor.switch1_example_port2_ap1_speed")
    assert speed.state == "1G"
    assert "10G" in speed.attributes["options"]

    poe = hass.states.get("sensor.switch1_example_port1_tnr0_poe_status")
    assert poe.state == "powered on"

    optical = hass.states.get("sensor.switch1_example_sfp_1_switch0_tx_power")
    assert float(optical.state) == pytest.approx(-2.5, abs=0.01)
    assert optical.attributes["unit_of_measurement"] == "dBm"

    firmware = hass.states.get("update.switch1_example_swos_lite_firmware")
    assert firmware.state == "off"
    assert firmware.attributes["installed_version"] == "2.21"
