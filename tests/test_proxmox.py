"""Proxmox VE backend tests."""

from __future__ import annotations

import time

import pytest

from monitorha.app.api.proxmox import ProxmoxClient

NOW = time.time()

ROUTES = {
    ("GET", "/api2/json/version"): {
        "data": {"version": "8.2.4", "release": "8.2", "repoid": "faa83925c9641325"}
    },
    ("GET", "/api2/json/cluster/status"): {
        "data": [
            {"type": "cluster", "name": "homelab", "version": 2, "nodes": 2, "quorate": 1},
            {"type": "node", "name": "pve1", "online": 1, "local": 1, "nodeid": 1},
            {"type": "node", "name": "pve2", "online": 0, "local": 0, "nodeid": 2},
        ]
    },
    ("GET", "/api2/json/cluster/resources"): {
        "data": [
            {
                "type": "node",
                "node": "pve1",
                "status": "online",
                "cpu": 0.12,
                "maxcpu": 16,
                "mem": 17179869184,
                "maxmem": 68719476736,
                "uptime": 864000,
            },
            {
                "type": "qemu",
                "vmid": 101,
                "name": "docker-host",
                "node": "pve1",
                "status": "running",
                "cpu": 0.34,
                "maxcpu": 4,
                "mem": 4294967296,
                "maxmem": 8589934592,
                "disk": 0,
                "maxdisk": 107374182400,
                "uptime": 432000,
                "template": 0,
            },
            {
                "type": "lxc",
                "vmid": 200,
                "name": "nginx",
                "node": "pve1",
                "status": "stopped",
                "cpu": 0,
                "maxcpu": 2,
                "mem": 0,
                "maxmem": 1073741824,
                "disk": 2147483648,
                "maxdisk": 8589934592,
                "template": 0,
            },
            {
                "type": "qemu",
                "vmid": 900,
                "name": "ubuntu-template",
                "node": "pve1",
                "status": "stopped",
                "template": 1,
            },
            {
                # Lives on the other node: present in cluster scope, filtered
                # out in node scope.
                "type": "qemu",
                "vmid": 300,
                "name": "other-node-vm",
                "node": "pve2",
                "status": "running",
                "cpu": 0.1,
                "maxcpu": 2,
                "mem": 1073741824,
                "maxmem": 2147483648,
                "disk": 0,
                "maxdisk": 10737418240,
                "uptime": 3600,
                "template": 0,
            },
            {
                "type": "storage",
                "storage": "local-zfs",
                "node": "pve1",
                "status": "available",
                "disk": 500107862016,
                "maxdisk": 1000215724032,
                "plugintype": "zfspool",
                "shared": 0,
            },
            {
                "type": "storage",
                "storage": "backup-nfs",
                "node": "pve1",
                "status": "available",
                "disk": 2199023255552,
                "maxdisk": 4398046511104,
                "plugintype": "nfs",
                "shared": 1,
            },
            {
                "type": "storage",
                "storage": "backup-nfs",
                "node": "pve2",
                "status": "available",
                "disk": 2199023255552,
                "maxdisk": 4398046511104,
                "plugintype": "nfs",
                "shared": 1,
            },
        ]
    },
    ("GET", "/api2/json/nodes/pve1/status"): {
        "data": {
            "uptime": 864000,
            "loadavg": ["0.42", "0.55", "0.61"],
            "cpu": 0.1234,
            "wait": 0.0021,
            "cpuinfo": {"cpus": 16, "model": "AMD Ryzen 9 5950X", "sockets": 1},
            "memory": {"used": 17179869184, "total": 68719476736, "free": 51539607552},
            "swap": {"used": 0, "total": 8589934592, "free": 8589934592},
            "rootfs": {"used": 21474836480, "total": 107374182400},
            "kversion": "Linux 6.8.12-1-pve",
            "pveversion": "pve-manager/8.2.4/faa83925c9641325",
        }
    },
    ("GET", "/api2/json/nodes/pve1/apt/update"): {
        "data": [
            {
                "Package": "pve-manager",
                "Version": "8.2.7",
                "OldVersion": "8.2.4",
                "Title": "Proxmox VE management toolkit",
                "ChangeLogUrl": "https://example.invalid/changelog",
                "Origin": "Proxmox",
            },
            {"Package": "libc6", "Version": "2.36-9+deb12u8", "OldVersion": "2.36-9+deb12u7"},
        ]
    },
    ("GET", "/api2/json/nodes/pve1/disks/list"): {
        "data": [
            {
                "devpath": "/dev/nvme0n1",
                "health": "PASSED",
                "model": "Samsung SSD 980 PRO",
                "serial": "S5GXNX0T123456",
                "size": 1000204886016,
                "type": "nvme",
                "wearout": 97,
            },
            {
                "devpath": "/dev/sda",
                "health": "FAILED",
                "model": "WDC WD40EFRX",
                "size": 4000787030016,
                "type": "hdd",
                "wearout": "N/A",
            },
            {"devpath": "/dev/sdb", "health": "UNKNOWN", "type": "hdd"},
        ]
    },
    ("GET", "/api2/json/nodes/pve1/disks/zfs"): {
        "data": [
            {
                "name": "rpool",
                "health": "ONLINE",
                "size": 1000215724032,
                "free": 500107862016,
                "alloc": 500107862016,
                "frag": 12,
                "dedup": 1.0,
            },
            {
                "name": "tank",
                "health": "DEGRADED",
                "size": 8000000000000,
                "free": 2000000000000,
                "frag": 31,
            },
        ]
    },
    ("GET", "/api2/json/nodes/pve1/certificates/info"): {
        "data": [
            {
                "filename": "pveproxy-ssl.pem",
                "notafter": int(NOW + 86400 * 45),
                "issuer": "CN=Let's Encrypt",
                "subject": "CN=pve1.example.invalid",
            }
        ]
    },
    ("GET", "/api2/json/nodes/pve1/tasks?typefilter=vzdump&limit=50"): {
        "data": [
            {
                "upid": "UPID:pve1:0000A1B2::vzdump::root@pam:",
                "node": "pve1",
                "type": "vzdump",
                "starttime": int(NOW - 7200),
                "endtime": int(NOW - 3600),
                "status": "OK",
                "user": "root@pam",
            },
            {
                "upid": "UPID:pve1:00009999::vzdump::root@pam:",
                "node": "pve1",
                "type": "vzdump",
                "starttime": int(NOW - 93600),
                "endtime": int(NOW - 90000),
                "status": "job errors",
                "user": "root@pam",
            },
        ]
    },
    ("GET", "/api2/json/storage"): {
        "data": [
            {"storage": "local-zfs", "type": "zfspool", "content": "images,rootdir"},
            {"storage": "backup-nfs", "type": "nfs", "content": "backup,iso", "shared": 1},
            {"storage": "old-backup", "type": "dir", "content": "backup", "disable": 1},
        ]
    },
    ("GET", "/api2/json/nodes/pve1/storage/backup-nfs/content?content=backup"): {
        "data": [
            {
                "volid": "backup-nfs:backup/vzdump-qemu-101-2026_02_10.vma.zst",
                "ctime": int(NOW - 172800),
                "size": 12884901888,
                "vmid": 101,
                "format": "vma.zst",
            },
            {
                "volid": "backup-nfs:backup/vzdump-qemu-101-2026_02_12.vma.zst",
                "ctime": int(NOW - 3600),
                "size": 13958643712,
                "vmid": 101,
                "format": "vma.zst",
                "protected": 1,
            },
        ]
    },
    ("GET", "/api2/json/nodes/pve1/network"): {
        "data": [
            {"iface": "lo", "type": "loopback", "method": "loopback", "active": 1},
            {
                "iface": "enp1s0",
                "type": "eth",
                "method": "manual",
                "active": 1,
                "autostart": 1,
            },
            {
                "iface": "vmbr0",
                "type": "bridge",
                "method": "static",
                "active": 1,
                "autostart": 1,
                "cidr": "192.0.2.100/24",
                "gateway": "192.0.2.10",
                "bridge_ports": "enp1s0",
            },
            {
                "iface": "vmbr1",
                "type": "bridge",
                "method": "manual",
                "autostart": 0,
                "bridge_ports": "none",
            },
        ]
    },
    ("GET", "/api2/json/nodes/pve2/network"): {"data": []},
}


def _client(session, **kwargs):
    return ProxmoxClient(
        session,
        "192.0.2.100",
        8006,
        token_id="monitoring@pve!homeassistant",
        token_secret="00000000-0000-0000-0000-000000000000",
        **kwargs,
    )


@pytest.fixture
def client(make_session):
    """The default: only the node we connect to (pve1 is the `local` one)."""
    return _client(make_session(ROUTES))


@pytest.fixture
def cluster_client(make_session):
    """Opt-in cluster scope: every node, plus the cluster device itself."""
    return _client(make_session(ROUTES), scope="cluster")


@pytest.mark.asyncio
async def test_devices_and_hierarchy(client):
    """Node scope: one device for the node, with its guests hanging off it."""
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.devices["main"].name == "pve1"
    assert snapshot.devices["main"].sw_version == "8.2.4"
    assert snapshot.devices["main"].via_device is None
    # No cluster device, and no device for any other node.
    assert "node_pve1" not in snapshot.devices
    assert "node_pve2" not in snapshot.devices

    assert snapshot.devices["guest_101"].via_device == "main"
    assert snapshot.devices["guest_101"].model == "QEMU virtual machine"
    assert snapshot.devices["guest_200"].model == "LXC container"
    # Templates are not real guests.
    assert "guest_900" not in snapshot.devices


@pytest.mark.asyncio
async def test_node_scope_ignores_the_rest_of_the_cluster(client):
    snapshot = await client.async_fetch(slow=True)
    # The cluster's own entities are gone.
    assert "nodes_online" not in snapshot.sensors
    assert "quorate" not in snapshot.binary_sensors
    # pve2 is in the fixture but must contribute nothing.
    assert "node_pve2_online" not in snapshot.binary_sensors
    assert not [k for k in snapshot.sensors if "pve2" in k]
    assert not [k for k in snapshot.binary_sensors if "pve2" in k]
    # A guest running on pve2 is not ours to report.
    assert "guest_300" not in snapshot.devices
    assert "guest_300_running" not in snapshot.binary_sensors
    assert snapshot.meta["node"] == "pve1"
    # No cluster_id, so the manager will not deduplicate two nodes.
    assert "cluster_id" not in snapshot.meta


@pytest.mark.asyncio
async def test_node_interfaces(client):
    snapshot = await client.async_fetch(slow=True)

    bridge = snapshot.binary_sensors["main_iface_vmbr0"]
    assert bridge.value is True
    assert bridge.device_key == "main"
    assert bridge.attributes["type"] == "bridge"
    assert bridge.attributes["cidr"] == "192.0.2.100/24"
    assert bridge.attributes["bridge_ports"] == "enp1s0"
    assert bridge.attributes["autostart"] is True

    assert snapshot.binary_sensors["main_iface_enp1s0"].value is True
    # Configured but not up: PVE simply omits `active`.
    assert snapshot.binary_sensors["main_iface_vmbr1"].value is False
    # Loopback exists on every node and says nothing useful.
    assert "main_iface_lo" not in snapshot.binary_sensors


@pytest.mark.asyncio
async def test_interfaces_can_be_turned_off(make_session):
    client = _client(make_session(ROUTES), monitor_interfaces=False)
    snapshot = await client.async_fetch(slow=True)
    assert not [k for k in snapshot.binary_sensors if "_iface_" in k]


@pytest.mark.asyncio
async def test_an_explicit_node_overrides_detection(make_session):
    """Needed when the host is a VIP that can answer as any member."""
    client = _client(make_session(ROUTES), node="pve2")
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.devices["main"].name == "pve2"
    assert snapshot.meta["node"] == "pve2"


@pytest.mark.asyncio
async def test_cluster_scope_keeps_the_old_hierarchy(cluster_client):
    snapshot = await cluster_client.async_fetch(slow=True)
    assert snapshot.devices["main"].name == "homelab"
    assert snapshot.devices["node_pve1"].via_device == "main"
    assert snapshot.devices["node_pve1"].sw_version == "8.2.4"
    # Guests hang off the node they run on, not off the cluster.
    assert snapshot.devices["guest_101"].via_device == "node_pve1"
    # The other node and its guest are reported here, unlike in node scope.
    assert snapshot.devices["guest_300"].via_device == "node_pve2"
    assert snapshot.meta["cluster_id"] == "homelab"


@pytest.mark.asyncio
async def test_cluster_and_node_health(cluster_client):
    snapshot = await cluster_client.async_fetch(slow=True)
    assert snapshot.sensors["nodes_online"].value == 1
    assert snapshot.sensors["nodes_online"].attributes["offline"] == ["pve2"]
    # quorate == 1 means no problem.
    assert snapshot.binary_sensors["quorate"].value is False
    assert snapshot.binary_sensors["node_pve1_online"].value is True
    assert snapshot.binary_sensors["node_pve2_online"].value is False

    assert snapshot.sensors["node_pve1_cpu"].value == pytest.approx(12.34)
    assert snapshot.sensors["node_pve1_memory"].value == pytest.approx(25.0)
    assert snapshot.sensors["node_pve1_rootfs"].value == pytest.approx(20.0)
    assert snapshot.sensors["node_pve1_load_1m"].value == 0.42


@pytest.mark.asyncio
async def test_offline_node_gets_no_telemetry(cluster_client):
    snapshot = await cluster_client.async_fetch(slow=True)
    assert "node_pve2_cpu" not in snapshot.sensors


@pytest.mark.asyncio
async def test_updates(client):
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.sensors["main_updates"].value == 2
    assert snapshot.binary_sensors["main_updates_available"].value is True

    update = snapshot.updates["main_pve_update"]
    assert update.installed_version == "8.2.4"
    assert update.latest_version == "8.2.7"
    # Proxmox exposes no upgrade endpoint, so this must stay read-only.
    assert update.install is None


@pytest.mark.asyncio
async def test_disk_and_zfs_health(client):
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.binary_sensors["main_disk_nvme0n1_health"].value is False
    assert snapshot.binary_sensors["main_disk_sda_health"].value is True
    # An unknown SMART state is not a failure report.
    assert snapshot.binary_sensors["main_disk_sdb_health"].value is None
    assert snapshot.sensors["main_disk_nvme0n1_wearout"].value == 97.0

    assert snapshot.binary_sensors["main_zfs_rpool_health"].value is False
    assert snapshot.binary_sensors["main_zfs_tank_health"].value is True
    assert snapshot.sensors["main_zfs_rpool_used"].value == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_storage_dedupes_shared(cluster_client):
    snapshot = await cluster_client.async_fetch(slow=True)
    # backup-nfs is shared and appears once per node; only one entity results.
    assert "storage_backup-nfs_used" in snapshot.sensors
    assert "storage_pve2_backup-nfs_used" not in snapshot.sensors
    assert snapshot.sensors["storage_pve1_local-zfs_used"].value == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_backups(client):
    snapshot = await client.async_fetch(slow=True)
    last = snapshot.sensors["guest_101_last_backup"]
    assert last.value is not None
    # Two archives exist for 101; the newest wins.
    assert last.attributes["backup_count"] == 2
    assert last.attributes["size_bytes"] == 13958643712
    assert last.attributes["protected"] is True

    # Guest 200 has never been backed up.
    assert snapshot.sensors["guest_200_last_backup"].value is None
    assert snapshot.sensors["guests_without_backup"].value == 1

    assert snapshot.binary_sensors["main_last_backup_failed"].value is False
    assert snapshot.sensors["main_last_backup_job"].attributes["status"] == "OK"


@pytest.mark.asyncio
async def test_guest_power_control(client):
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.binary_sensors["guest_101_running"].value is True
    assert snapshot.switches["guest_101_power"].value is True
    assert snapshot.switches["guest_200_power"].value is False

    await snapshot.switches["guest_200_power"].turn(True)
    method, path, _ = client._session.calls[-1]
    assert (method, path) == ("POST", "/api2/json/nodes/pve1/lxc/200/status/start")

    await snapshot.switches["guest_101_power"].turn(False)
    assert client._session.calls[-1][1] == (
        "/api2/json/nodes/pve1/qemu/101/status/shutdown"
    )

    await snapshot.buttons["guest_101_force_stop"].press()
    assert client._session.calls[-1][1] == "/api2/json/nodes/pve1/qemu/101/status/stop"


@pytest.mark.asyncio
async def test_token_auth_header(client):
    await client.async_fetch(slow=False)
    headers = client._headers()
    assert headers["Authorization"] == (
        "PVEAPIToken=monitoring@pve!homeassistant="
        "00000000-0000-0000-0000-000000000000"
    )


@pytest.mark.asyncio
async def test_slow_data_survives_fast_polls(client):
    await client.async_fetch(slow=True)
    client._session.calls.clear()

    snapshot = await client.async_fetch(slow=False)
    # No deep endpoints were re-fetched...
    assert not [c for c in client._session.calls if "apt/update" in c[1]]
    assert not [c for c in client._session.calls if "disks/list" in c[1]]
    # ...but their entities still report the cached values.
    assert snapshot.sensors["main_updates"].value == 2
    assert snapshot.binary_sensors["main_disk_sda_health"].value is True


@pytest.mark.asyncio
async def test_standalone_host_without_cluster(make_session):
    routes = dict(ROUTES)
    routes[("GET", "/api2/json/cluster/status")] = {
        "data": [{"type": "node", "name": "pve1", "online": 1, "local": 1}]
    }
    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    snapshot = await client.async_fetch(slow=True)
    # In node scope the node *is* the main device, so it carries the node name.
    assert snapshot.devices["main"].name == "pve1"
    assert snapshot.devices["main"].via_device is None
    # A standalone host has no quorum concept.
    assert "quorate" not in snapshot.binary_sensors


@pytest.mark.asyncio
async def test_validate(client):
    """Node scope identifies the node, so each node is its own entry."""
    info = await client.async_validate()
    assert info["unique_id"] == "node-pve1"
    assert info["title"] == "pve1"


@pytest.mark.asyncio
async def test_validate_in_cluster_scope(cluster_client):
    info = await cluster_client.async_validate()
    assert info["unique_id"] == "cluster-homelab"
    assert info["title"] == "homelab"


# -- partial privileges --------------------------------------------------


@pytest.mark.asyncio
async def test_403_on_optional_endpoint_degrades(make_session):
    """A token without rights to one subsystem must not kill the whole poll.

    PVEAuditor does not cover every endpoint this client touches, so a 403 on
    an optional one has to leave the rest of the source working.
    """
    from tests.conftest import Status

    routes = dict(ROUTES)
    routes[("GET", "/api2/json/nodes/pve1/apt/update")] = Status(403)
    routes[("GET", "/api2/json/nodes/pve1/disks/list")] = Status(403)

    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    snapshot = await client.async_fetch(slow=True)

    # Everything the token *can* read is still there.
    assert snapshot.sensors["main_cpu"].value == pytest.approx(12.34)
    assert snapshot.binary_sensors["main_zfs_tank_health"].value is True
    # The forbidden subsystems simply report nothing.
    assert snapshot.sensors["main_updates"].value == 0
    assert "main_disk_sda_health" not in snapshot.binary_sensors


@pytest.mark.asyncio
async def test_403_on_required_endpoint_still_raises(make_session):
    """A 403 on a core endpoint is a real permissions problem, not a shrug."""
    from monitorha.app.api.base import AuthenticationError
    from tests.conftest import Status

    routes = dict(ROUTES)
    routes[("GET", "/api2/json/cluster/resources")] = Status(403)

    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    with pytest.raises(AuthenticationError) as err:
        await client.async_fetch(slow=True)
    # The message must name the endpoint so the missing privilege is findable.
    assert "cluster/resources" in str(err.value)
    assert "403" in str(err.value)


@pytest.mark.asyncio
async def test_timeout_on_optional_endpoint_degrades(make_session):
    """A slow subsystem must not take the whole source down.

    disks/list shells out to smartctl and is proxied when it targets another
    node, so it can time out on a perfectly healthy cluster.
    """
    routes = dict(ROUTES)
    routes[("GET", "/api2/json/nodes/pve1/disks/list")] = TimeoutError()

    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    snapshot = await client.async_fetch(slow=True)

    assert snapshot.sensors["main_cpu"].value == pytest.approx(12.34)
    assert snapshot.binary_sensors["main_zfs_tank_health"].value is True
    assert "main_disk_sda_health" not in snapshot.binary_sensors


@pytest.mark.asyncio
async def test_timeout_on_required_endpoint_still_raises(make_session):
    from monitorha.app.api.base import ConnectionFailed

    routes = dict(ROUTES)
    routes[("GET", "/api2/json/cluster/resources")] = TimeoutError()

    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    with pytest.raises(ConnectionFailed) as err:
        await client.async_fetch(slow=True)
    assert "Timeout" in str(err.value)


@pytest.mark.asyncio
async def test_slow_tier_keeps_last_good_value_on_failure(make_session):
    """One bad cycle must not blank entities that were working."""
    routes = dict(ROUTES)
    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    first = await client.async_fetch(slow=True)
    assert first.binary_sensors["main_disk_sda_health"].value is True

    # The disk inventory starts timing out; its entities keep their values.
    client._session.routes[("GET", "/api2/json/nodes/pve1/disks/list")] = TimeoutError()
    second = await client.async_fetch(slow=True)
    assert second.binary_sensors["main_disk_sda_health"].value is True


@pytest.mark.asyncio
async def test_slow_endpoints_get_a_longer_timeout(make_session):
    """The deep tier must not inherit the 20s default."""
    from monitorha.app.api.proxmox import _SLOW_TIMEOUT

    client = ProxmoxClient(
        make_session(dict(ROUTES)), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    await client.async_fetch(slow=True)
    assert _SLOW_TIMEOUT > 20


# -- saying what the problem is ------------------------------------------


async def test_quorum_loss_names_the_missing_nodes(make_session) -> None:
    import copy

    routes = copy.deepcopy(ROUTES)
    routes[("GET", "/api2/json/cluster/status")] = {
        "data": [
            {"type": "cluster", "name": "homelab", "nodes": 3, "quorate": 0},
            {"type": "node", "name": "pve1", "online": 1, "local": 1},
            {"type": "node", "name": "pve2", "online": 0},
            {"type": "node", "name": "pve3", "online": 0},
        ]
    }
    client = ProxmoxClient(
        make_session(routes),
        "192.0.2.100",
        8006,
        token_id="monitoring@pve!ha",
        token_secret="x",
        scope="cluster",
    )
    snapshot = await client.async_fetch(slow=True)
    quorum = snapshot.binary_sensors["quorate"]
    assert quorum.value is True  # PROBLEM is on: no quorum
    assert quorum.reason == "No quorum: 1 of 3 nodes online, missing pve2, pve3"


async def test_a_quorate_cluster_needs_no_explanation(cluster_client) -> None:
    snapshot = await cluster_client.async_fetch(slow=True)
    quorum = snapshot.binary_sensors["quorate"]
    assert quorum.value is False
    assert quorum.reason is None


async def test_a_package_refresh_button_is_offered(client) -> None:
    """`apt update` is the half Proxmox exposes; the upgrade stays manual."""
    snapshot = await client.async_fetch(slow=True)
    assert "main_apt_refresh" in snapshot.buttons
    assert snapshot.updates["main_pve_update"].install is None


@pytest.mark.asyncio
async def test_403_on_node_status_raises_rather_than_blanking(make_session):
    """The call every node reading depends on is not an optional subsystem.

    An ACL that stops at `/` leaves `/cluster/status` and `/cluster/resources`
    answering while `/nodes/{node}/status` returns 403. Swallowing that
    produced a node that looked healthy with six empty readings and nothing in
    the log, which is the failure this test exists to prevent coming back.
    """
    from monitorha.app.api.base import AuthenticationError
    from tests.conftest import Status

    routes = dict(ROUTES)
    routes[("GET", "/api2/json/nodes/pve1/status")] = Status(
        403, {"message": "Permission check failed (/nodes/pve1, Sys.Audit)"}
    )

    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    with pytest.raises(AuthenticationError) as err:
        await client.async_fetch(slow=True)

    message = str(err.value)
    assert "nodes/pve1/status" in message
    assert "Sys.Audit" in message
    # The cause seen in the wild leads: a row on /nodes replaces the grant
    # inherited from /, so "being specific" silently removes access.
    assert "pveum acl list" in message
    assert "replaces" in message
    # The other two causes still get a mention.
    assert "propagate" in message
    assert "privilege separation" in message


@pytest.mark.asyncio
async def test_a_wedged_node_status_degrades_quietly(make_session, caplog):
    """A 5xx is transient, so it must not take the whole source down."""
    from tests.conftest import Status

    routes = dict(ROUTES)
    routes[("GET", "/api2/json/nodes/pve1/status")] = Status(500)

    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    with caplog.at_level("WARNING"):
        snapshot = await client.async_fetch(slow=True)

    # Everything sourced elsewhere survives.
    assert snapshot.binary_sensors["main_zfs_tank_health"].value is True
    assert snapshot.devices["guest_101"].name.startswith("docker-host")
    # The status-derived readings are empty, but they say so in the log.
    assert snapshot.sensors["main_memory"].value is None
    assert "No status from Proxmox node pve1" in caplog.text

    # And it is said once, not once per poll.
    caplog.clear()
    await client.async_fetch(slow=False)
    assert "No status from Proxmox node pve1" not in caplog.text


@pytest.mark.asyncio
async def test_a_timeout_on_node_status_degrades_too(make_session):
    routes = dict(ROUTES)
    routes[("GET", "/api2/json/nodes/pve1/status")] = TimeoutError()

    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.binary_sensors["main_online"].value is True
    # Only the readings with no other source go blank; CPU and last boot still
    # have the `/cluster/resources` node entry to fall back on.
    assert snapshot.sensors["main_memory"].value is None
    assert snapshot.sensors["main_kernel"].value is None
    assert snapshot.sensors["main_cpu"].value == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_validate_fails_when_the_token_cannot_read_the_node(make_session):
    """`/version` alone answers for a powerless token, so it proves nothing.

    Test used to pass on credentials that could not produce one reading.
    """
    from monitorha.app.api.base import AuthenticationError
    from tests.conftest import Status

    routes = dict(ROUTES)
    routes[("GET", "/api2/json/nodes/pve1/status")] = Status(403)

    client = ProxmoxClient(
        make_session(routes), "192.0.2.100", 8006, token_id="a!b", token_secret="c"
    )
    with pytest.raises(AuthenticationError):
        await client.async_validate()


@pytest.mark.asyncio
async def test_validate_still_succeeds_on_a_healthy_node(client):
    info = await client.async_validate()
    assert info["unique_id"] == "node-pve1"
    assert info["title"] == "pve1"


@pytest.mark.asyncio
async def test_an_offline_node_is_not_polled_for_status(make_session):
    """Node scope used to force the target online and poll a downed node."""
    client = _client(make_session(ROUTES), node="pve2")
    snapshot = await client.async_fetch(slow=True)

    assert snapshot.binary_sensors["main_online"].value is False
    assert not any(
        path.endswith("/nodes/pve2/status") for _, path, _ in client._session.calls
    )
