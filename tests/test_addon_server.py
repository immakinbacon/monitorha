"""Add-on tests: config store, HTTP API, auth and the polling manager."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from monitorha.app.manager import Manager, SourceRunner
from monitorha.app.server import create_app
from monitorha.app.store import ConfigError, Store, validate_source

from .test_mikrotik import ROUTES as MIKROTIK_ROUTES

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


# -- store ---------------------------------------------------------------


def test_store_generates_and_persists_token(tmp_path: Path) -> None:
    first = Store(tmp_path / "config.json")
    assert len(first.api_token) > 20
    # A restart must not invalidate the integration's credentials.
    assert Store(tmp_path / "config.json").api_token == first.api_token


def test_store_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ this is not json")
    store = Store(path)
    assert store.sources == []
    assert store.api_token


def test_store_crud(store: Store) -> None:
    source = store.add(MIKROTIK_SOURCE)
    assert source["id"]
    assert source["port"] == 443  # filled in from the type default
    assert store.get(source["id"])["name"] == "core-router"

    store.update(source["id"], {"name": "renamed"})
    assert store.get(source["id"])["name"] == "renamed"

    store.remove(source["id"])
    assert store.sources == []
    with pytest.raises(ConfigError):
        store.remove(source["id"])


def test_blank_secret_on_edit_keeps_stored_value(store: Store) -> None:
    """The UI never receives the password back, so blank must mean unchanged."""
    source = store.add(MIKROTIK_SOURCE)
    store.update(source["id"], {"name": "renamed", "password": ""})
    assert store.get(source["id"])["password"] == "secret"


def test_validate_rejects_bad_input() -> None:
    with pytest.raises(ConfigError):
        validate_source({"type": "nonsense", "host": "x"})
    with pytest.raises(ConfigError):
        validate_source({"type": "mikrotik", "host": ""})
    with pytest.raises(ConfigError):
        # Proxmox token auth needs a token ID.
        validate_source({"type": "proxmox", "host": "x", "auth_method": "token"})


def test_intervals_are_clamped() -> None:
    source = validate_source(
        {**MIKROTIK_SOURCE, "scan_interval": 1, "slow_scan_interval": 5}
    )
    assert source["scan_interval"] == 10
    assert source["slow_scan_interval"] == 60


def test_secrets_are_written_but_not_returned(store: Store, tmp_path: Path) -> None:
    from monitorha.app.store import redact

    source = store.add(MIKROTIK_SOURCE)
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["sources"][0]["password"] == "secret"
    assert redact(source)["password"] == "__stored__"


# -- HTTP API ------------------------------------------------------------


@pytest.fixture
async def client(store: Store, make_session, monkeypatch, socket_enabled) -> TestClient:
    """An add-on server whose backends talk to canned device responses.

    `socket_enabled` lifts the Home Assistant test harness's global socket ban
    so the aiohttp test server can bind a loopback port.
    """
    session = make_session(MIKROTIK_ROUTES)
    # Both the polling manager and the /api/sources/test endpoint build their
    # own sessions, so both call sites need redirecting.
    for target in ("monitorha.app.manager", "monitorha.app.server"):
        monkeypatch.setattr(f"{target}.make_session", lambda verify_ssl: session)
    manager = Manager(store)
    app = create_app(store, manager)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()
    await manager.stop()


def auth(store: Store) -> dict[str, str]:
    return {"Authorization": f"Bearer {store.api_token}"}


async def test_health_needs_no_auth(client: TestClient) -> None:
    response = await client.get("/api/health")
    assert response.status == 200
    assert (await response.json())["ok"] is True


async def test_api_requires_token(client: TestClient) -> None:
    assert (await client.get("/api/snapshot")).status == 401
    assert (
        await client.get("/api/snapshot", headers={"Authorization": "Bearer wrong"})
    ).status == 401


async def test_ingress_requests_bypass_the_token(client: TestClient) -> None:
    """Supervisor Ingress has already authenticated the Home Assistant user."""
    response = await client.get(
        "/api/snapshot", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"}
    )
    assert response.status == 200


async def test_add_source_starts_polling(client: TestClient, store: Store) -> None:
    response = await client.post(
        "/api/sources", json=MIKROTIK_SOURCE, headers=auth(store)
    )
    assert response.status == 201
    created = await response.json()
    # The API must never hand back a credential.
    assert created["password"] == "__stored__"

    listing = await (await client.get("/api/sources", headers=auth(store))).json()
    assert len(listing["sources"]) == 1
    assert listing["api_token"] == store.api_token


async def test_listing_names_the_updates_a_device_is_waiting_on(
    client: TestClient, store: Store
) -> None:
    """What the device card renders its "updates pending" badge from."""
    await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    manager: Manager = client.app["manager"]
    runner = next(iter(manager.runners.values()))
    await runner._poll_once()

    listing = await (await client.get("/api/sources", headers=auth(store))).json()
    pending = listing["sources"][0]["status"]["pending_updates"]
    # The fixture router is on RouterOS 7.14.3 with 7.15 published, and on a
    # RouterBOARD firmware with a newer one bundled.
    assert pending == ["RouterOS", "RouterBOARD firmware"]


async def test_snapshot_exposes_entities(client: TestClient, store: Store) -> None:
    await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    manager: Manager = client.app["manager"]
    # Poll deterministically rather than waiting for the background loop.
    runner = next(iter(manager.runners.values()))
    await runner._poll_once()

    snapshot = await (await client.get("/api/snapshot", headers=auth(store))).json()
    source = snapshot["sources"][0]
    assert source["available"] is True
    assert source["name"] == "core-router"

    sensors = {s["key"]: s for s in source["sensors"]}
    assert sensors["cpu_load"]["value"] == 7.0
    assert sensors["health_temperature"]["unit"] == "°C"
    assert sensors["health_temperature"]["device_class"] == "temperature"
    # Timestamps cross the wire as ISO strings.
    assert isinstance(sensors["last_boot"]["value"], str)

    binaries = {b["key"]: b for b in source["binary_sensors"]}
    assert binaries["health_psu2_state"]["value"] is True

    updates = {u["key"]: u for u in source["updates"]}
    assert updates["routeros_update"]["latest_version"] == "7.15"
    # Callables cannot be serialised; capability becomes a flag.
    assert updates["routeros_update"]["can_install"] is True

    switches = {s["key"]: s for s in source["switches"]}
    assert switches["poe_ether3"]["value"] is True


async def test_action_reaches_the_device(client: TestClient, store: Store, make_session) -> None:
    await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    manager: Manager = client.app["manager"]
    runner = next(iter(manager.runners.values()))
    await runner._poll_once()

    response = await client.post(
        "/api/action",
        json={
            "source_id": runner.id,
            "kind": "switch",
            "key": "poe_ether4",
            "value": True,
        },
        headers=auth(store),
    )
    assert response.status == 200

    # The action queues a refresh, so the PATCH is not necessarily the last
    # call recorded.
    calls = runner._client._session.calls
    assert (
        "PATCH",
        "/rest/interface/ethernet/poe/*4",
        {"poe-out": "auto-on"},
    ) in calls


async def test_unknown_action_is_a_clean_error(client: TestClient, store: Store) -> None:
    await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    manager: Manager = client.app["manager"]
    runner = next(iter(manager.runners.values()))
    await runner._poll_once()

    response = await client.post(
        "/api/action",
        json={"source_id": runner.id, "kind": "button", "key": "nope"},
        headers=auth(store),
    )
    assert response.status == 500
    assert "No such button" in (await response.json())["error"]


async def test_test_endpoint_validates_without_saving(
    client: TestClient, store: Store
) -> None:
    response = await client.post(
        "/api/sources/test", json=MIKROTIK_SOURCE, headers=auth(store)
    )
    assert response.status == 200
    assert (await response.json())["info"]["title"] == "core-router"
    assert store.sources == []


async def test_delete_stops_the_runner(client: TestClient, store: Store) -> None:
    created = await (
        await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    ).json()
    manager: Manager = client.app["manager"]
    assert len(manager.runners) == 1

    await client.delete(f"/api/sources/{created['id']}", headers=auth(store))
    assert manager.runners == {}


async def test_disabled_source_is_not_polled(client: TestClient, store: Store) -> None:
    created = await (
        await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    ).json()
    manager: Manager = client.app["manager"]
    await client.put(
        f"/api/sources/{created['id']}", json={"enabled": False}, headers=auth(store)
    )
    assert manager.runners == {}


# -- runner behaviour ----------------------------------------------------


async def test_failed_poll_records_error_without_crashing(make_session) -> None:
    """A device that answers nothing must not take down the loop."""
    runner = SourceRunner(validate_source(MIKROTIK_SOURCE) | {"id": "x"})
    await runner._session.close()
    runner._session = make_session({})
    from monitorha.app.api import build_client

    runner._client = build_client(runner.config, runner._session)

    await runner._poll_once()
    assert runner.error is not None
    assert runner.available is False
    payload = runner.as_dict()
    # Still serialisable, just empty.
    assert payload["sensors"] == []
    assert payload["error"]
    await runner._session.close()


async def test_source_snapshot_endpoint(client: TestClient, store: Store) -> None:
    """The detail page reads one source rather than the whole fleet."""
    created = await (
        await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    ).json()
    manager: Manager = client.app["manager"]
    runner = manager.runners[created["id"]]
    await runner._poll_once()

    response = await client.get(
        f"/api/sources/{created['id']}/snapshot", headers=auth(store)
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["id"] == created["id"]
    assert payload["name"] == "core-router"

    # Everything the detail page groups by device must be present.
    assert {d["key"] for d in payload["devices"]} == {"main"}
    assert any(s["key"] == "cpu_load" for s in payload["sensors"])
    assert any(b["key"] == "health_psu2_state" for b in payload["binary_sensors"])
    assert any(s["key"] == "poe_ether3" for s in payload["switches"])
    assert any(b["key"] == "reboot" for b in payload["buttons"])
    # Every reading names the device it belongs to, or the page cannot group it.
    for collection in ("sensors", "binary_sensors", "switches", "updates", "buttons"):
        assert all(item["device_key"] for item in payload[collection])


async def test_source_snapshot_unknown_id(client: TestClient, store: Store) -> None:
    response = await client.get("/api/sources/nope/snapshot", headers=auth(store))
    assert response.status == 500
    assert "not running" in (await response.json())["error"]


# -- host normalisation --------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "host", "port"),
    [
        # The case that prompted this: a URL pasted straight from the browser.
        ("https://bmc.example.com/", "bmc.example.com", 443),
        ("https://pve.example.internal:8006/", "pve.example.internal", 8006),
        ("http://192.0.2.10", "192.0.2.10", 443),
        ("bmc.example.internal", "bmc.example.internal", 443),
        ("192.0.2.10:8443", "192.0.2.10", 8443),
        ("  192.0.2.10  ", "192.0.2.10", 443),
        ("[2001:db8::1]:8443", "2001:db8::1", 8443),
        # A bare IPv6 address must not be mistaken for host:port.
        ("2001:db8::1", "2001:db8::1", 443),
    ],
)
def test_host_field_accepts_urls_and_ports(typed, host, port) -> None:
    source = validate_source(
        {"type": "redfish", "host": typed, "username": "ADMIN", "password": "x"}
    )
    assert source["host"] == host
    assert source["port"] == port


def test_port_in_host_beats_the_port_box() -> None:
    """The port box is prefilled, so an explicit one in the URL wins."""
    source = validate_source(
        {
            "type": "proxmox",
            "host": "https://pve.example.internal:8007/",
            "port": 8006,
            "token_id": "a!b",
            "token_secret": "c",
        }
    )
    assert source["port"] == 8007


def test_port_box_used_when_host_has_none() -> None:
    source = validate_source(
        {
            "type": "redfish",
            "host": "bmc.example.internal",
            "port": 8443,
            "username": "ADMIN",
            "password": "x",
        }
    )
    assert source["port"] == 8443


def test_url_only_host_is_still_rejected_when_empty() -> None:
    with pytest.raises(ConfigError):
        validate_source({"type": "redfish", "host": "https://", "username": "a"})


# -- events and overrides over HTTP --------------------------------------


async def test_events_endpoint_reports_a_head_when_empty(
    client: TestClient, store: Store
) -> None:
    """A fresh consumer needs somewhere to start from, not an error."""
    response = await client.get("/api/events", headers=auth(store))
    assert response.status == 200
    body = await response.json()
    assert body == {"head": 0, "events": []}


async def test_events_endpoint_is_a_cursor(client: TestClient, store: Store) -> None:
    from monitorha.app.events import Event

    manager = client.server.app["manager"]
    manager.events.append(
        [
            Event(kind="problem", source_id="a", source_name="a", entity_key="x", name="x"),
            Event(kind="recovery", source_id="a", source_name="a", entity_key="y", name="y"),
        ]
    )

    body = await (await client.get("/api/events", headers=auth(store))).json()
    assert [e["entity_key"] for e in body["events"]] == ["x", "y"]
    assert body["head"] == 2

    body = await (await client.get("/api/events?since=1", headers=auth(store))).json()
    assert [e["entity_key"] for e in body["events"]] == ["y"]


async def test_events_endpoint_rejects_a_bad_cursor(
    client: TestClient, store: Store
) -> None:
    response = await client.get("/api/events?since=soon", headers=auth(store))
    assert response.status == 400


async def test_events_endpoint_needs_auth(client: TestClient) -> None:
    assert (await client.get("/api/events")).status == 401


async def test_override_round_trip_over_http(
    client: TestClient, store: Store
) -> None:
    created = await (
        await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    ).json()
    source_id = created["id"]

    response = await client.put(
        f"/api/sources/{source_id}/overrides/cpu_temp",
        json={"muted": True, "thresholds": {"warn_above": 70}},
        headers=auth(store),
    )
    assert response.status == 200
    assert (await response.json())["muted"] is True

    listed = await (
        await client.get(f"/api/sources/{source_id}/overrides", headers=auth(store))
    ).json()
    assert listed["overrides"]["cpu_temp"]["thresholds"] == {"warn_above": 70.0}

    assert (
        await client.delete(
            f"/api/sources/{source_id}/overrides/cpu_temp", headers=auth(store)
        )
    ).status == 200
    listed = await (
        await client.get(f"/api/sources/{source_id}/overrides", headers=auth(store))
    ).json()
    assert listed["overrides"] == {}


async def test_setting_an_override_does_not_restart_the_poller(
    client: TestClient, store: Store
) -> None:
    """The runner must survive a threshold edit, snapshot and all."""
    created = await (
        await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    ).json()
    manager = client.server.app["manager"]
    runner_before = manager.runners[created["id"]]

    await client.put(
        f"/api/sources/{created['id']}/overrides/cpu_temp",
        json={"thresholds": {"warn_above": 70}},
        headers=auth(store),
    )
    await manager.sync()

    assert manager.runners[created["id"]] is runner_before


async def test_bad_threshold_is_rejected(client: TestClient, store: Store) -> None:
    created = await (
        await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    ).json()
    response = await client.put(
        f"/api/sources/{created['id']}/overrides/cpu_temp",
        json={"thresholds": {"warn_above": "hot"}},
        headers=auth(store),
    )
    assert response.status == 400


async def test_overrides_reach_the_snapshot(client: TestClient, store: Store) -> None:
    """The UI reads a line's settings from the same payload as its value."""
    created = await (
        await client.post("/api/sources", json=MIKROTIK_SOURCE, headers=auth(store))
    ).json()
    await client.put(
        f"/api/sources/{created['id']}/overrides/cpu_temp",
        json={"muted": True},
        headers=auth(store),
    )
    body = await (
        await client.get(
            f"/api/sources/{created['id']}/snapshot", headers=auth(store)
        )
    ).json()
    assert body["overrides"]["cpu_temp"]["muted"] is True


# -- version -------------------------------------------------------------


def test_the_three_version_numbers_agree() -> None:
    """config.yaml, manifest.json and the app constant must not drift.

    The add-on image contains only `app/`, so the running code cannot read
    config.yaml and carries its own copy of the number. If they disagree, the
    version shown in the UI is a lie — which is exactly what makes "am I
    running the new build?" unanswerable.
    """
    import json
    import re

    root = Path(__file__).resolve().parents[1]
    config = (root / "monitorha" / "config.yaml").read_text()
    declared = re.search(r'^version:\s*"([^"]+)"', config, re.MULTILINE).group(1)
    manifest = json.loads(
        (root / "custom_components" / "monitorha" / "manifest.json").read_text()
    )

    from monitorha.app.const import VERSION

    assert VERSION == declared
    assert manifest["version"] == declared


async def test_health_reports_the_version(client: TestClient) -> None:
    """Answers "which build is this?" without shell access."""
    from monitorha.app.const import VERSION

    body = await (await client.get("/api/health")).json()
    assert body["version"] == VERSION


# -- asset caching -------------------------------------------------------


async def test_asset_urls_carry_the_version(client: TestClient) -> None:
    """A new release must be a new URL, or the browser keeps the old script."""
    from monitorha.app.const import VERSION

    body = await (await client.get("/")).text()
    assert f"static/app.js?v={VERSION}" in body
    assert f"static/style.css?v={VERSION}" in body
    assert "__VERSION__" not in body


async def test_the_page_itself_is_never_cached(client: TestClient) -> None:
    """Otherwise the stamped asset URLs inside it are never seen."""
    response = await client.get("/")
    assert response.headers["Cache-Control"] == "no-cache"


async def test_static_assets_must_revalidate(client: TestClient) -> None:
    """aiohttp sends an ETag but no Cache-Control, and a browser without one
    applies heuristic freshness — serving a stale script without asking."""
    response = await client.get("/static/app.js")
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers.get("Etag")
