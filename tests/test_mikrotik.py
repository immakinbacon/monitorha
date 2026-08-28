"""RouterOS backend tests, using payloads shaped like real RouterOS v7 output."""

from __future__ import annotations

import pytest

from monitorha.app.api.mikrotik import MikrotikClient, parse_timespan

ROUTES = {
    ("GET", "/rest/system/resource"): {
        "uptime": "6w4d17:20:41",
        "version": "7.14.3 (stable)",
        "build-time": "2024-05-14 10:23:33",
        "factory-software": "7.1",
        "free-memory": "838860800",
        "total-memory": "1073741824",
        "cpu": "ARM64",
        "cpu-count": "4",
        "cpu-frequency": "1400",
        "cpu-load": "7",
        "free-hdd-space": "83886080",
        "total-hdd-space": "134217728",
        "architecture-name": "arm64",
        "board-name": "RB5009UG+S+",
        "platform": "MikroTik",
        "bad-blocks": "0",
    },
    ("GET", "/rest/system/identity"): {"name": "core-router"},
    ("GET", "/rest/system/routerboard"): {
        "model": "RB5009UG+S+",
        "serial-number": "HEX123456789",
        "firmware-type": "rb5009",
        "factory-firmware": "7.1",
        "current-firmware": "7.14.3",
        "upgrade-firmware": "7.15",
    },
    ("GET", "/rest/system/health"): [
        {".id": "*1", "name": "temperature", "type": "C", "value": "43"},
        {".id": "*2", "name": "cpu-temperature", "type": "C", "value": "51"},
        {".id": "*3", "name": "voltage", "type": "V", "value": "24.1"},
        {".id": "*4", "name": "fan1-speed", "type": "RPM", "value": "4200"},
        {".id": "*5", "name": "psu1-state", "value": "ok"},
        {".id": "*6", "name": "psu2-state", "value": "fail"},
    ],
    ("GET", "/rest/system/package/update"): {
        "channel": "stable",
        "installed-version": "7.14.3",
        "latest-version": "7.15",
        "status": "New version is available",
    },
    ("GET", "/rest/interface"): [
        {
            "name": "ether1",
            "type": "ether",
            "running": "true",
            "disabled": "false",
            "rx-byte": "918273645",
            "tx-byte": "112233445",
            "mac-address": "AA:BB:CC:DD:EE:01",
            "mtu": "1500",
        },
        {
            "name": "ether2",
            "type": "ether",
            "running": "false",
            "disabled": "true",
            "rx-byte": "0",
            "tx-byte": "0",
        },
        {"name": "lo", "type": "loopback", "running": "true", "disabled": "false"},
    ],
    ("GET", "/rest/interface/ethernet/poe"): [
        {".id": "*3", "name": "ether3", "poe-out": "auto-on"},
        {".id": "*4", "name": "ether4", "poe-out": "off"},
    ],
    ("GET", "/rest/ip/firewall/connection/tracking"): {
        "total-entries": "482",
        "max-entries": "131072",
    },
    ("GET", "/rest/interface/wireguard"): [
        {
            "name": "wg-site",
            "running": "true",
            "disabled": "false",
            "listen-port": "13231",
            "public-key": "aBcDeF0123456789",
            "comment": "site-to-site",
        },
        {"name": "wg-old", "running": "false", "disabled": "true"},
    ],
    ("GET", "/rest/interface/wireguard/peers"): [
        {
            "interface": "wg-site",
            "name": "office",
            "public-key": "PEER1KEY0000",
            "endpoint-address": "203.0.113.7",
            "allowed-address": "10.9.0.0/24",
            "last-handshake": "1m12s",
            "rx": "104857600",
            "tx": "52428800",
            "disabled": "false",
        },
        {
            "interface": "wg-site",
            "comment": "laptop",
            "public-key": "PEER2KEY0000",
            # Rekeys every two minutes, so 22 minutes of silence is down.
            "last-handshake": "22m4s",
            "rx": "10",
            "tx": "20",
            "disabled": "false",
        },
        {
            "interface": "wg-site",
            "public-key": "PEER3KEY0000NEVERUP",
            "disabled": "false",
        },
    ],
    ("GET", "/rest/interface/ovpn-client"): [
        {
            "name": "ovpn-backup",
            "running": "false",
            "disabled": "false",
            "connect-to": "vpn.example.internal",
            "user": "router",
        }
    ],
    ("GET", "/rest/interface/l2tp-client"): [
        {"name": "l2tp-out1", "running": "true", "disabled": "false"}
    ],
    ("GET", "/rest/ip/ipsec/active-peers"): [
        {
            "id": "*1",
            "local-address": "192.0.2.1",
            "remote-address": "198.51.100.5",
            "state": "established",
            "uptime": "3d4h12m",
        }
    ],
    ("GET", "/rest/ip/ipsec/policy"): [
        {
            "src-address": "10.0.0.0/24",
            "dst-address": "10.9.0.0/24",
            "ph2-state": "established",
            "disabled": "false",
        },
        {
            "src-address": "10.0.0.0/24",
            "dst-address": "10.8.0.0/24",
            "ph2-state": "no-phase2",
            "disabled": "false",
        },
        # RouterOS ships a template policy that never establishes.
        {"dst-address": "0.0.0.0/0", "template": "true", "disabled": "false"},
    ],
    ("GET", "/rest/ppp/active"): [
        {"name": "alice", "service": "ovpn", "address": "10.9.0.5"},
        {"name": "bob", "service": "ovpn", "address": "10.9.0.6"},
        {"name": "site-b", "service": "l2tp", "address": "10.9.0.9"},
    ],
    ("GET", "/rest/tool/netwatch"): [
        {
            "host": "8.8.8.8",
            "comment": "Google DNS",
            "type": "icmp",
            "status": "up",
            "since": "2026-08-13 09:14:02",
            "rtt-avg": "12.4ms",
            "loss-percent": "0%",
            "disabled": "false",
        },
        {
            "host": "10.0.0.99",
            "type": "icmp",
            "status": "down",
            "since": "2026-08-13 11:02:44",
            "disabled": "false",
        },
        {"host": "10.0.0.50", "status": "up", "disabled": "true"},
    ],
}


@pytest.fixture
def client(make_session):
    session = make_session(ROUTES)
    client = MikrotikClient(session, "192.0.2.10", 443, "monitor", "secret")
    client.session = session
    return client


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6w4d17:20:41", 6 * 604800 + 4 * 86400 + 17 * 3600 + 20 * 60 + 41),
        ("3d10:11:12", 3 * 86400 + 10 * 3600 + 11 * 60 + 12),
        ("17:20:41", 17 * 3600 + 20 * 60 + 41),
        ("1h2m3s", 3723),
        ("45s", 45),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_timespan(text, expected):
    assert parse_timespan(text) == expected


@pytest.mark.asyncio
async def test_device_metadata(client):
    snapshot = await client.async_fetch(slow=True)
    device = snapshot.devices["main"]
    assert device.name == "core-router"
    assert device.manufacturer == "MikroTik"
    assert device.model == "RB5009UG+S+"
    assert device.serial_number == "HEX123456789"
    assert device.sw_version == "7.14.3 (stable)"


@pytest.mark.asyncio
async def test_resource_sensors(client):
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.sensors["cpu_load"].value == 7.0
    # 1 GiB total, 800 MiB free -> 200 MiB used
    assert snapshot.sensors["memory_used_percent"].value == pytest.approx(21.88, abs=0.1)
    assert snapshot.sensors["disk_used_percent"].value == pytest.approx(37.5, abs=0.1)
    assert snapshot.sensors["last_boot"].value is not None
    assert snapshot.sensors["connections"].value == 482


@pytest.mark.asyncio
async def test_health_typing(client):
    snapshot = await client.async_fetch(slow=True)
    temp = snapshot.sensors["health_temperature"]
    assert temp.value == 43.0
    assert temp.unit == "°C"
    assert snapshot.sensors["health_fan1_speed"].value == 4200.0
    assert snapshot.sensors["health_voltage"].value == 24.1

    # PROBLEM binary sensors are on when the PSU is *not* ok.
    assert snapshot.binary_sensors["health_psu1_state"].value is False
    assert snapshot.binary_sensors["health_psu2_state"].value is True


@pytest.mark.asyncio
async def test_updates(client):
    snapshot = await client.async_fetch(slow=True)
    routeros = snapshot.updates["routeros_update"]
    assert routeros.installed_version == "7.14.3"
    assert routeros.latest_version == "7.15"
    assert routeros.install is not None

    firmware = snapshot.updates["routerboard_firmware"]
    assert firmware.installed_version == "7.14.3"
    assert firmware.latest_version == "7.15"


@pytest.mark.asyncio
async def test_interfaces_skip_disabled_and_loopback(client):
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.binary_sensors["iface_ether1_running"].value is True
    assert snapshot.sensors["iface_ether1_rx_bytes"].value == 918273645.0
    # ether2 is administratively disabled and lo is a loopback.
    assert "iface_ether2_running" not in snapshot.binary_sensors
    assert "iface_lo_running" not in snapshot.binary_sensors


@pytest.mark.asyncio
async def test_poe_switches_and_control(client):
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.switches["poe_ether3"].value is True
    assert snapshot.switches["poe_ether4"].value is False

    await snapshot.switches["poe_ether4"].turn(True)
    method, path, body = client._session.calls[-1]
    assert (method, path) == ("PATCH", "/rest/interface/ethernet/poe/*4")
    assert body == {"poe-out": "auto-on"}

    await snapshot.switches["poe_ether3"].turn(False)
    assert client._session.calls[-1][2] == {"poe-out": "off"}


@pytest.mark.asyncio
async def test_update_check_only_runs_on_slow_tier(client):
    await client.async_fetch(slow=True)
    checks = [c for c in client._session.calls if "check-for-updates" in c[1]]
    assert len(checks) == 1

    client._session.calls.clear()
    await client.async_fetch(slow=False)
    assert not [c for c in client._session.calls if "check-for-updates" in c[1]]


@pytest.mark.asyncio
async def test_missing_optional_endpoints_are_tolerated(make_session):
    """A device without health, PoE or conntrack must still produce a snapshot."""
    minimal = {
        k: v
        for k, v in ROUTES.items()
        if k[1]
        in ("/rest/system/resource", "/rest/system/identity", "/rest/system/routerboard")
    }
    session = make_session(minimal)
    client = MikrotikClient(session, "192.0.2.10", 443, "monitor", "secret")
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.sensors["cpu_load"].value == 7.0
    assert not snapshot.switches
    assert "connections" not in snapshot.sensors


@pytest.mark.asyncio
async def test_validate(client):
    info = await client.async_validate()
    assert info["unique_id"] == "HEX123456789"
    assert info["title"] == "core-router"


async def test_a_failed_psu_says_so(client) -> None:
    """"Problem" on its own does not tell you which PSU or how it failed."""
    snapshot = await client.async_fetch(slow=True)
    failed = snapshot.binary_sensors["health_psu2_state"]
    assert failed.value is True
    assert failed.reason == "Psu2 state reports fail"
    # A healthy one carries no explanation to show.
    assert snapshot.binary_sensors["health_psu1_state"].reason is None


# -- tunnels -------------------------------------------------------------


async def test_wireguard_interfaces_are_tracked(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.binary_sensors["wg_wg_site_running"].value is True
    # A disabled interface is configuration, not a fault to report.
    assert "wg_wg_old_running" not in snapshot.binary_sensors


async def test_a_wireguard_peer_is_up_while_it_keeps_handshaking(client) -> None:
    """WireGuard has no session; recent handshakes are the only evidence."""
    snapshot = await client.async_fetch(slow=True)
    peer = snapshot.binary_sensors["wg_peer_wg_site_office_connected"]
    assert peer.value is True
    assert peer.reason is None
    assert snapshot.sensors["wg_peer_wg_site_office_handshake_age"].value == 72


async def test_a_silent_wireguard_peer_reads_as_down(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    peer = snapshot.binary_sensors["wg_peer_wg_site_laptop_connected"]
    assert peer.value is False
    assert peer.reason == "No handshake for 1324s"


async def test_a_peer_that_never_connected_is_down_not_unknown(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    peer = snapshot.binary_sensors["wg_peer_wg_site_peer3key0000_connected"]
    assert peer.value is False
    assert peer.reason == "No handshake recorded"


async def test_peer_traffic_counters_are_available_but_off_by_default(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    rx = snapshot.sensors["wg_peer_wg_site_office_rx_bytes"]
    assert rx.value == 104857600
    assert rx.enabled_default is False


async def test_tunnel_clients_report_connectivity(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    ovpn = snapshot.binary_sensors["ovpn_ovpn_backup_connected"]
    assert ovpn.value is False
    assert ovpn.reason == "OpenVPN client ovpn-backup is not connected"
    assert snapshot.binary_sensors["l2tp_l2tp_out1_connected"].value is True


async def test_ipsec_peers_and_policies(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    assert (
        snapshot.binary_sensors["ipsec_peer_198_51_100_5_established"].value is True
    )
    broken = snapshot.binary_sensors[
        "ipsec_policy_10_0_0_0_24_10_8_0_0_24_installed"
    ]
    assert broken.value is False
    assert broken.reason == "Phase 2 is no-phase2"


async def test_the_ipsec_template_policy_is_ignored(client) -> None:
    """It never establishes, so it would be a permanent false alarm."""
    snapshot = await client.async_fetch(slow=True)
    assert not [
        k for k in snapshot.binary_sensors if k.startswith("ipsec_policy_none_0_0_0_0")
    ]


async def test_dial_in_sessions_are_counted_not_enumerated(client) -> None:
    """One entity per session would churn the registry as users come and go."""
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.sensors["ppp_active_total"].value == 3
    assert snapshot.sensors["ppp_active_ovpn"].value == 2
    assert snapshot.sensors["ppp_active_ovpn"].attributes["users"] == ["alice", "bob"]


# -- netwatch ------------------------------------------------------------


async def test_netwatch_hosts_become_connectivity_entities(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    up = snapshot.binary_sensors["netwatch_google_dns_up"]
    assert up.value is True
    assert up.attributes["host"] == "8.8.8.8"


async def test_a_down_netwatch_host_says_since_when(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    down = snapshot.binary_sensors["netwatch_10_0_0_99_up"]
    assert down.value is False
    assert down.reason == "10.0.0.99 has been down since 2026-08-13 11:02:44"


async def test_netwatch_timings_are_parsed(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.sensors["netwatch_google_dns_rtt"].value == 12.4
    assert snapshot.sensors["netwatch_google_dns_loss"].value == 0.0
    # A non-ICMP probe reports no timings, and must not invent them.
    assert "netwatch_10_0_0_99_rtt" not in snapshot.sensors


async def test_a_disabled_netwatch_entry_is_skipped(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    assert "netwatch_10_0_0_50_up" not in snapshot.binary_sensors


# -- opting out ----------------------------------------------------------


async def test_tunnels_and_netwatch_can_be_turned_off(make_session) -> None:
    client = MikrotikClient(
        make_session(ROUTES),
        "192.0.2.10",
        443,
        "monitor",
        "secret",
        monitor_tunnels=False,
        monitor_netwatch=False,
    )
    snapshot = await client.async_fetch(slow=True)
    assert not [k for k in snapshot.binary_sensors if k.startswith(("wg_", "netwatch_"))]
    assert "ppp_active_total" not in snapshot.sensors


async def test_a_router_without_these_menus_still_polls(make_session) -> None:
    """An older RouterOS, or one without the WireGuard package, 404s these."""
    routes = {
        k: v
        for k, v in ROUTES.items()
        if not any(
            part in k[1]
            for part in ("wireguard", "ovpn", "l2tp", "ipsec", "ppp", "netwatch")
        )
    }
    client = MikrotikClient(make_session(routes), "192.0.2.10", 443, "monitor", "secret")
    snapshot = await client.async_fetch(slow=True)
    # The rest of the poll is unaffected.
    assert snapshot.sensors["cpu_load"] is not None


async def test_a_check_for_updates_button_is_offered(client) -> None:
    """The scheduled check only runs on the slow tier; this forces one."""
    snapshot = await client.async_fetch(slow=True)
    assert "check_for_updates" in snapshot.buttons
