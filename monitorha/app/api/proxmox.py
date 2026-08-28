"""Proxmox VE backend.

By default a config entry covers **one node**: the node whose API you connect
to, its interfaces, storages and the guests running on it.  Home Assistant sees
a device for the node with a device per guest hanging off it.  Nothing about
the wider cluster is reported, so pointing an entry at each node you care about
gives one device tree per node.

Set `scope` to `cluster` for the older behaviour, where a single entry
discovers every node in the cluster and publishes a cluster device carrying
quorum and node-count sensors.

Authentication prefers an API token (`user@realm!tokenid` + secret) because it
never expires and can be scoped to PVEAuditor.  Username/password is supported
as a fallback and transparently re-tickets every two hours.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import quote

import aiohttp


from ..const import (
    BinarySensorDeviceClass,
    ButtonDeviceClass,
    EntityCategory,
    PERCENTAGE,
    SensorDeviceClass,
    SensorStateClass,
    SwitchDeviceClass,
    UnitOfInformation,
)
from ..const import AUTH_TOKEN, MAIN
from ..models import (
    BinaryReading,
    ButtonSpec,
    DeviceMeta,
    Reading,
    Snapshot,
    SwitchSpec,
    UpdateReading,
)
from .base import (
    AuthenticationError,
    BaseClient,
    ConnectionFailed,
    percent,
    to_float,
    to_int,
)

_LOGGER = logging.getLogger(__name__)

_GUEST_TYPES = ("qemu", "lxc")

# Monitor only the node we connect to, or the whole cluster it belongs to.
SCOPE_NODE = "node"
SCOPE_CLUSTER = "cluster"
SCOPES = (SCOPE_NODE, SCOPE_CLUSTER)

# The deep-tier endpoints are slow by nature: the disk inventory shells out to
# smartctl (and spins up sleeping drives), and listing backup content walks a
# whole storage. A request for another node is also proxied through the node
# this client is connected to, which adds a hop. The default 20s is not enough.
_SLOW_TIMEOUT = 90.0

# A node can be up but refuse a subsystem (no ZFS, no subscription, a token
# without rights to that one endpoint); none of that must fail the poll.
_SOFT_STATUSES = (400, 403, 404, 500, 501, 596)
# The narrower set, for a call the entry cannot do without. A wedged or
# unimplemented endpoint still degrades, but 401/403/404 mean the entry is
# misconfigured and will never fix itself, so they are reported.
_TRANSIENT_STATUSES = (500, 501, 596)

_HEALTHY_DISK = {"PASSED", "OK"}
_HEALTHY_ZFS = {"ONLINE"}
_TICKET_LIFETIME = 7000  # PVE tickets last 2h; refresh a little early.


def _epoch(value: Any) -> datetime | None:
    seconds = to_float(value)
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, UTC)


class ProxmoxClient(BaseClient):
    """Proxmox VE API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        *,
        auth_method: str = AUTH_TOKEN,
        username: str = "",
        password: str = "",
        token_id: str = "",
        token_secret: str = "",
        monitor_guests: bool = True,
        monitor_backups: bool = True,
        monitor_interfaces: bool = True,
        scope: str = SCOPE_NODE,
        node: str = "",
    ) -> None:
        super().__init__(session, host, port, use_ssl=True)
        self._auth_method = auth_method
        self._username = username
        self._password = password
        self._token_id = token_id
        self._token_secret = token_secret
        self._monitor_guests = monitor_guests
        self._monitor_backups = monitor_backups
        self._monitor_interfaces = monitor_interfaces
        self._scope = scope if scope in SCOPES else SCOPE_NODE
        # Empty means "whichever node we are talking to", which is what you
        # want unless the host is a VIP that can land on any member.
        self._node = node.strip()

        self._ticket: str | None = None
        self._csrf: str | None = None
        self._ticket_at: float = 0.0
        # Nodes whose status call is currently coming back empty, so the
        # warning is logged on the way in and not once per poll thereafter.
        self._degraded: set[str] = set()
        # Slow-tier results are cached so their entities keep a value between
        # the deep polls.
        self._slow: dict[str, Any] = {}

    # -- transport --------------------------------------------------------

    @property
    def _api(self) -> str:
        return f"{self.base_url}/api2/json"

    async def _ensure_ticket(self) -> None:
        if self._auth_method == AUTH_TOKEN:
            return
        if self._ticket and (time.monotonic() - self._ticket_at) < _TICKET_LIFETIME:
            return
        result = await self._request(
            "POST",
            f"{self._api}/access/ticket",
            data={"username": self._username, "password": self._password},
        )
        data = (result or {}).get("data") or {}
        if not data.get("ticket"):
            raise AuthenticationError("Proxmox rejected the username/password")
        self._ticket = data["ticket"]
        self._csrf = data.get("CSRFPreventionToken")
        self._ticket_at = time.monotonic()

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        if self._auth_method == AUTH_TOKEN:
            return {
                "Authorization": (
                    f"PVEAPIToken={self._token_id}={self._token_secret}"
                )
            }
        headers = {"Cookie": f"PVEAuthCookie={self._ticket}"}
        if write and self._csrf:
            headers["CSRFPreventionToken"] = self._csrf
        return headers

    async def _get(
        self,
        path: str,
        *,
        soft: bool = False,
        transient: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """GET an API path and unwrap the `data` envelope.

        `soft` tolerates everything a node can throw at an *optional*
        subsystem, including a 403, and returns None instead.

        `transient` is the middle setting, for a call the entry depends on:
        being slow, wedged or unimplemented still degrades quietly, but an
        authorisation or not-found answer is reported. Swallowing those is how
        a token that cannot read `/nodes/{node}/status` ends up looking like a
        healthy node with five blank readings.
        """
        await self._ensure_ticket()
        result = await self._request(
            "GET",
            f"{self._api}{path}",
            headers=self._headers(),
            allow_status=(
                _SOFT_STATUSES if soft else _TRANSIENT_STATUSES if transient else ()
            ),
            timeout=timeout,
            # A subsystem that is merely slow or wedged must not fail the poll
            # either, whichever tier it is on.
            optional=soft or transient,
        )
        return None if result is None else result.get("data")

    async def _node_status(self, node: str) -> dict[str, Any]:
        """The node's core stats: CPU, memory, swap, rootfs, load, kernel.

        Every one of those readings comes from this single call, so it is not
        treated as optional. A 403 here is the signature of an ACL that does
        not reach `/nodes/{node}`, which is worth saying out loud because
        Proxmox's own message is easy to misread as a role problem when it is
        usually a propagation or privilege-separation one.
        """
        try:
            result = await self._get(
                f"/nodes/{quote(node, safe='')}/status", transient=True
            )
        except AuthenticationError as err:
            raise AuthenticationError(
                f"{err} Proxmox wants Sys.Audit on /nodes/{node}. Run "
                "`pveum acl list`: a permission on /nodes replaces the one "
                "inherited from / rather than adding to it, so a row there "
                "with a role lacking Sys.Audit removes the access a grant on "
                "/ was providing. Failing that, check the / permission is set "
                "to propagate, and that a token with privilege separation "
                "carries it on both the token and its user."
            ) from err
        if result:
            self._degraded.discard(node)
            return result
        if node not in self._degraded:
            # Reaching here means a 5xx or a timeout, since the authorisation
            # statuses now raise. Transient, but it blanks six readings at
            # once, so it earns one line rather than six silent dashes.
            _LOGGER.warning(
                "No status from Proxmox node %s: its CPU, memory, swap, root "
                "filesystem, load and kernel readings will be empty until it "
                "answers again",
                node,
            )
            self._degraded.add(node)
        return {}

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        await self._ensure_ticket()
        result = await self._request(
            "POST",
            f"{self._api}{path}",
            headers=self._headers(write=True),
            data=payload or {},
        )
        return None if result is None else result.get("data")

    # -- scope ------------------------------------------------------------

    @property
    def node_scoped(self) -> bool:
        return self._scope == SCOPE_NODE

    def _node_device_key(self, node: str) -> str:
        """In node scope the node *is* the main device.

        In cluster scope the main device represents the cluster and each node
        hangs off it, so the node needs a key of its own.
        """
        return MAIN if self.node_scoped else f"node_{node}"

    def _resolve_node(
        self,
        node_entries: list[dict[str, Any]],
        resources: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Work out which node this entry monitors."""
        if self._node:
            return self._node

        # `local: 1` marks the node actually answering the request, which is
        # the right answer whenever we connect straight to a node.
        local = next(
            (
                str(n["name"])
                for n in node_entries
                if n.get("name") and to_int(n.get("local")) == 1
            ),
            None,
        )
        if local:
            return local

        names = [str(n["name"]) for n in node_entries if n.get("name")]
        if not names:
            names = [
                str(r["node"])
                for r in (resources or [])
                if isinstance(r, dict) and r.get("type") == "node" and r.get("node")
            ]
        if not names:
            return None
        if len(names) > 1:
            # No node claimed to be local, which happens behind a proxy or VIP.
            _LOGGER.warning(
                "Could not tell which Proxmox node is being monitored "
                "(candidates: %s); using %s. Set 'node' to choose explicitly.",
                ", ".join(sorted(names)),
                names[0],
            )
        return names[0]

    @staticmethod
    def _only_node(
        resources: list[dict[str, Any]], node: str
    ) -> list[dict[str, Any]]:
        """Drop everything in /cluster/resources belonging to other nodes.

        Shared storages are listed once per node, so the copy carrying our node
        survives and cluster-wide storage still appears.
        """
        return [
            r
            for r in resources
            if isinstance(r, dict) and r.get("node") == node
        ]

    # -- config flow ------------------------------------------------------

    async def async_validate(self) -> dict[str, Any]:
        version = await self._get("/version")
        if not isinstance(version, dict):
            raise ConnectionFailed("Unexpected response from /api2/json/version")
        status = await self._get("/cluster/status", soft=True) or []
        node_entries = [
            e for e in status if isinstance(e, dict) and e.get("type") == "node"
        ]

        # `/version` answers for a token with no privileges at all, and
        # `/cluster/status` only needs Sys.Audit on `/`, so neither proves the
        # entry will produce a single reading. The node's own status is what
        # every node reading depends on, so test succeeds only if that works.
        resolved = self._resolve_node(node_entries)
        if resolved:
            await self._node_status(resolved)

        if self.node_scoped:
            # Identify by node, so each node in a cluster is a separate entry
            # rather than colliding on one cluster-wide unique id.
            name = resolved or self._host
            unique = f"node-{name}"
        else:
            cluster = next(
                (
                    e
                    for e in status
                    if isinstance(e, dict) and e.get("type") == "cluster"
                ),
                None,
            )
            if cluster:
                name = cluster.get("name") or "Proxmox cluster"
                unique = f"cluster-{name}"
            else:
                name = (node_entries[0].get("name") if node_entries else None) or (
                    self._host
                )
                unique = f"node-{name}"
        return {
            "unique_id": unique,
            "title": name,
            "version": version.get("version"),
        }

    # -- polling ----------------------------------------------------------

    async def async_fetch(self, *, slow: bool) -> Snapshot:
        snapshot = Snapshot()

        resources = await self._get("/cluster/resources") or []
        status = await self._get("/cluster/status", soft=True) or []
        version = await self._get("/version", soft=True) or {}

        cluster_entry = next(
            (e for e in status if isinstance(e, dict) and e.get("type") == "cluster"),
            None,
        )
        node_entries = [
            e for e in status if isinstance(e, dict) and e.get("type") == "node"
        ]
        node_names = [n["name"] for n in node_entries if n.get("name")]
        if not node_names:
            node_names = [
                r["node"]
                for r in resources
                if isinstance(r, dict) and r.get("type") == "node" and r.get("node")
            ]

        if self.node_scoped:
            target = self._resolve_node(node_entries, resources)
            if target is None:
                raise ConnectionFailed(
                    "Could not determine which Proxmox node to monitor"
                )
            node_names = [target]
            # Everything on another node is somebody else's problem now.
            resources = self._only_node(resources, target)
            node_entries = [n for n in node_entries if n.get("name") == target]
            snapshot.meta["node"] = target
            # Deliberately no cluster_id: the manager only deduplicates hosts
            # reporting a whole cluster, and here each host reports only itself,
            # so two nodes of one cluster must both publish.
        elif cluster_entry and cluster_entry.get("name"):
            # `/cluster/resources` and `/cluster/status` answer identically on
            # every node, so two configured hosts in one cluster would publish
            # the same nodes, guests, storage and quorum twice. Naming the
            # cluster lets the manager spot that and elect one reporter.
            snapshot.meta["cluster_id"] = str(cluster_entry["name"])
            local = next(
                (n.get("name") for n in node_entries if to_int(n.get("local")) == 1),
                None,
            )
            if local:
                snapshot.meta["local_node"] = str(local)

        if not self.node_scoped:
            cluster_name = (
                (cluster_entry or {}).get("name")
                or (node_names[0] if node_names else self._host)
            )
            snapshot.add_device(
                DeviceMeta(
                    key=MAIN,
                    name=cluster_name if cluster_entry else f"Proxmox {cluster_name}",
                    manufacturer="Proxmox",
                    model="Proxmox VE cluster" if cluster_entry else "Proxmox VE host",
                    sw_version=version.get("version"),
                    configuration_url=self.base_url,
                )
            )
            self._add_cluster(snapshot, cluster_entry, node_entries)

        online_nodes = [
            n["name"]
            for n in node_entries
            if n.get("name") and to_int(n.get("online")) == 1
        ]
        if not node_entries:
            # `/cluster/status` told us nothing, so there is no online flag to
            # believe and the nodes we know about are assumed up. When it did
            # answer, a node it calls offline stays offline — otherwise node
            # scope would poll a downed node's endpoints on every cycle.
            online_nodes = node_names

        if slow or not self._slow:
            await self._refresh_slow(online_nodes, resources)

        for node in node_names:
            online = node in online_nodes
            await self._add_node(
                snapshot,
                node,
                resources,
                online=online,
                device_key=self._node_device_key(node),
                fallback_version=version.get("version"),
            )

        self._add_storages(snapshot, resources)
        if self._monitor_guests:
            self._add_guests(snapshot, resources)
        if self._monitor_backups:
            self._add_backup_summary(snapshot, resources)

        return snapshot

    async def _refresh_slow(
        self, nodes: list[str], resources: list[dict[str, Any]]
    ) -> None:
        """Gather the expensive, slow-moving data."""
        for node in nodes:
            enc = quote(node, safe="")
            # Each of these keeps its previous value if it fails, so a node
            # that is slow this cycle does not blank out its entities.
            for key, path in (
                (f"apt/{node}", f"/nodes/{enc}/apt/update"),
                (f"disks/{node}", f"/nodes/{enc}/disks/list"),
                (f"zfs/{node}", f"/nodes/{enc}/disks/zfs"),
                (f"certs/{node}", f"/nodes/{enc}/certificates/info"),
                (
                    f"tasks/{node}",
                    f"/nodes/{enc}/tasks?typefilter=vzdump&limit=50",
                ),
            ):
                result = await self._get(path, soft=True, timeout=_SLOW_TIMEOUT)
                if result is None and key in self._slow:
                    _LOGGER.debug("Keeping previous %s; this poll returned nothing", key)
                    continue
                self._slow[key] = result or []
        if self._monitor_backups:
            self._slow["backups"] = await self._collect_backups(nodes, resources)

    async def _collect_backups(
        self, nodes: list[str], resources: list[dict[str, Any]]
    ) -> dict[int, dict[str, Any]]:
        """Build a vmid -> latest-backup map by listing backup storage content.

        vzdump *tasks* do not record which guests a job covered, so the backup
        archives themselves are the only reliable per-guest source.
        """
        storages = await self._get("/storage", soft=True) or []
        by_vmid: dict[int, dict[str, Any]] = {}

        for storage in storages:
            if not isinstance(storage, dict):
                continue
            if to_int(storage.get("disable")) == 1:
                continue
            if "backup" not in str(storage.get("content") or ""):
                continue
            sid = storage.get("storage")
            if not sid:
                continue

            # Honour a storage's node restriction, otherwise query any node.
            restricted = [n for n in str(storage.get("nodes") or "").split(",") if n]
            candidates = [n for n in nodes if not restricted or n in restricted]
            if not candidates:
                continue

            content = None
            for node in candidates:
                content = await self._get(
                    f"/nodes/{quote(node, safe='')}/storage/"
                    f"{quote(str(sid), safe='')}/content?content=backup",
                    soft=True,
                    # Walking a large NFS or PBS store takes a while.
                    timeout=_SLOW_TIMEOUT,
                )
                if content is not None:
                    break
                # Shared storages answer from any node; local ones only from
                # their owner, so a miss means "try the next node".
            if not isinstance(content, list):
                continue

            for item in content:
                if not isinstance(item, dict):
                    continue
                vmid = to_int(item.get("vmid"))
                ctime = to_float(item.get("ctime"))
                if vmid is None or ctime is None:
                    continue
                record = by_vmid.setdefault(vmid, {"count": 0, "ctime": 0.0})
                record["count"] += 1
                if ctime > record["ctime"]:
                    record.update(
                        ctime=ctime,
                        size=to_float(item.get("size")),
                        volid=item.get("volid"),
                        storage=sid,
                        protected=bool(to_int(item.get("protected"))),
                        verification=(item.get("verification") or {}).get("state"),
                    )
        return by_vmid

    # -- sections ---------------------------------------------------------

    def _add_cluster(
        self,
        snapshot: Snapshot,
        cluster: dict[str, Any] | None,
        nodes: list[dict[str, Any]],
    ) -> None:
        online = sum(1 for n in nodes if to_int(n.get("online")) == 1)
        snapshot.add(
            Reading(
                key="nodes_online",
                name="Nodes online",
                value=online,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:server",
                attributes={
                    "total": len(nodes),
                    "offline": [
                        n.get("name")
                        for n in nodes
                        if to_int(n.get("online")) != 1
                    ],
                },
            )
        )
        if cluster:
            quorate = to_int(cluster.get("quorate"))
            offline = [
                str(n.get("name")) for n in nodes if to_int(n.get("online")) != 1
            ]
            lost = quorate is not None and quorate != 1
            snapshot.add_binary(
                BinaryReading(
                    key="quorate",
                    name="Quorum",
                    # PROBLEM is on when there is a problem: no quorum.
                    value=None if quorate is None else quorate != 1,
                    reason=(
                        f"No quorum: {online} of {len(nodes)} nodes online"
                        + (f", missing {', '.join(offline)}" if offline else "")
                        if lost
                        else None
                    ),
                    device_class=BinarySensorDeviceClass.PROBLEM,
                    attributes={"nodes": to_int(cluster.get("nodes"))},
                )
            )

    async def _add_node(
        self,
        snapshot: Snapshot,
        node: str,
        resources: list[dict[str, Any]],
        *,
        online: bool,
        device_key: str,
        fallback_version: str | None = None,
    ) -> None:
        resource = next(
            (
                r
                for r in resources
                if isinstance(r, dict)
                and r.get("type") == "node"
                and r.get("node") == node
            ),
            {},
        )
        status = await self._node_status(node) if online else {}

        pve_version = status.get("pveversion") or ""
        snapshot.add_device(
            DeviceMeta(
                key=device_key,
                name=node,
                manufacturer="Proxmox",
                model=(status.get("cpuinfo") or {}).get("model") or "Proxmox VE node",
                sw_version=(
                    pve_version.split("/")[1]
                    if "/" in pve_version
                    else fallback_version
                ),
                configuration_url=f"{self.base_url}/#v1:0:=node%2F{node}",
                # In node scope this *is* the main device, so it has no parent.
                via_device=None if device_key == MAIN else MAIN,
            )
        )

        snapshot.add_binary(
            BinaryReading(
                key=f"{device_key}_online",
                name="Online",
                value=online,
                device_class=BinarySensorDeviceClass.CONNECTIVITY,
                device_key=device_key,
            )
        )

        if not online:
            return

        memory = status.get("memory") or {}
        swap = status.get("swap") or {}
        rootfs = status.get("rootfs") or {}
        loadavg = status.get("loadavg") or []

        cpu_fraction = to_float(status.get("cpu") or resource.get("cpu"))
        snapshot.add(
            Reading(
                key=f"{device_key}_cpu",
                name="CPU used",
                value=None if cpu_fraction is None else round(cpu_fraction * 100, 2),
                unit=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=1,
                icon="mdi:cpu-64-bit",
                device_key=device_key,
                attributes={"cores": to_int((status.get("cpuinfo") or {}).get("cpus"))},
            )
        )
        snapshot.add(
            Reading(
                key=f"{device_key}_io_wait",
                name="IO wait",
                value=(
                    None
                    if to_float(status.get("wait")) is None
                    else round(to_float(status.get("wait")) * 100, 2)
                ),
                unit=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=2,
                icon="mdi:timer-sand",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_key=device_key,
                enabled_default=False,
            )
        )
        snapshot.add(
            Reading(
                key=f"{device_key}_memory",
                name="Memory used",
                value=percent(memory.get("used"), memory.get("total")),
                unit=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=1,
                icon="mdi:memory",
                device_key=device_key,
                attributes={
                    "used_bytes": to_int(memory.get("used")),
                    "total_bytes": to_int(memory.get("total")),
                },
            )
        )
        snapshot.add(
            Reading(
                key=f"{device_key}_swap",
                name="Swap used",
                value=percent(swap.get("used"), swap.get("total")),
                unit=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=1,
                icon="mdi:swap-horizontal",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_key=device_key,
                enabled_default=False,
            )
        )
        snapshot.add(
            Reading(
                key=f"{device_key}_rootfs",
                name="Root filesystem used",
                value=percent(rootfs.get("used"), rootfs.get("total")),
                unit=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=1,
                icon="mdi:harddisk",
                device_key=device_key,
                attributes={
                    "used_bytes": to_int(rootfs.get("used")),
                    "total_bytes": to_int(rootfs.get("total")),
                },
            )
        )
        for index, window in enumerate(("1m", "5m", "15m")):
            if index >= len(loadavg):
                break
            snapshot.add(
                Reading(
                    key=f"{device_key}_load_{window}",
                    name=f"Load average {window}",
                    value=to_float(loadavg[index]),
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=2,
                    icon="mdi:chart-line",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    device_key=device_key,
                    enabled_default=index == 0,
                )
            )
        snapshot.add(
            Reading(
                key=f"{device_key}_last_boot",
                name="Last boot",
                value=self.boot_time(
                    device_key, to_float(status.get("uptime") or resource.get("uptime"))
                ),
                device_class=SensorDeviceClass.TIMESTAMP,
                entity_category=EntityCategory.DIAGNOSTIC,
                device_key=device_key,
            )
        )
        snapshot.add(
            Reading(
                key=f"{device_key}_kernel",
                name="Kernel",
                value=status.get("kversion"),
                icon="mdi:linux",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_key=device_key,
                enabled_default=False,
            )
        )

        self._add_node_updates(snapshot, node, device_key, pve_version)
        self._add_node_disks(snapshot, node, device_key)
        self._add_node_zfs(snapshot, node, device_key)
        self._add_node_certificates(snapshot, node, device_key)
        self._add_node_backup_jobs(snapshot, node, device_key)
        if self._monitor_interfaces:
            await self._add_node_interfaces(snapshot, node, device_key)

    async def _add_node_interfaces(
        self, snapshot: Snapshot, node: str, device_key: str
    ) -> None:
        """Physical NICs, bridges and bonds configured on the node.

        `/nodes/{node}/network` reports both the configuration and, for
        interfaces that are up, an `active` flag. It is a cheap call, so it
        stays on the fast tier where the active state is actually useful.
        """
        enc = quote(node, safe="")
        interfaces = await self._get(f"/nodes/{enc}/network", soft=True)
        if not isinstance(interfaces, list):
            return

        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            name = iface.get("iface")
            if not name:
                continue

            kind = str(iface.get("type") or "")
            # Loopback tells you nothing, and PVE reports it on every node.
            if kind == "loopback":
                continue

            attributes: dict[str, Any] = {
                "type": kind or None,
                "method": iface.get("method"),
                "autostart": to_int(iface.get("autostart")) == 1,
            }
            for source, key in (
                ("cidr", "cidr"),
                ("address", "address"),
                ("gateway", "gateway"),
                ("bridge_ports", "bridge_ports"),
                ("slaves", "bond_slaves"),
                ("bond_mode", "bond_mode"),
                ("comments", "comment"),
            ):
                value = iface.get(source)
                if value not in (None, ""):
                    attributes[key] = (
                        value.strip() if isinstance(value, str) else value
                    )

            # `active` is absent for interfaces that are configured but down.
            active = iface.get("active")
            snapshot.add_binary(
                BinaryReading(
                    key=f"{device_key}_iface_{name}",
                    name=f"Interface {name}",
                    value=to_int(active) == 1,
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    device_key=device_key,
                    attributes=attributes,
                )
            )

    def _add_node_updates(
        self, snapshot: Snapshot, node: str, device_key: str, pve_version: str
    ) -> None:
        packages = self._slow.get(f"apt/{node}") or []
        names = sorted(
            str(p.get("Package")) for p in packages if isinstance(p, dict) and p.get("Package")
        )
        snapshot.add(
            Reading(
                key=f"{device_key}_updates",
                name="Available updates",
                value=len(names),
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:package-up",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_key=device_key,
                # The full list can exceed the 16 KiB attribute limit on a
                # neglected host, so keep it bounded.
                attributes={"packages": names[:100], "truncated": len(names) > 100},
            )
        )
        snapshot.add_binary(
            BinaryReading(
                key=f"{device_key}_updates_available",
                name="Updates available",
                value=bool(names),
                reason=(
                    f"{len(names)} package(s) pending on {node}" if names else None
                ),
                device_class=BinarySensorDeviceClass.UPDATE,
                entity_category=EntityCategory.DIAGNOSTIC,
                device_key=device_key,
            )
        )
        snapshot.add_button(
            ButtonSpec(
                key=f"{device_key}_apt_refresh",
                name="Refresh package list",
                # `apt update` is the one half of this Proxmox does expose;
                # the upgrade itself deliberately has no API and stays manual.
                press=partial(
                    self._post, f"/nodes/{quote(node, safe='')}/apt/update"
                ),
                icon="mdi:package-up",
                entity_category=EntityCategory.CONFIG,
                device_key=device_key,
            )
        )

        installed = pve_version.split("/")[1] if "/" in pve_version else None
        pending = next(
            (
                p
                for p in packages
                if isinstance(p, dict) and p.get("Package") == "pve-manager"
            ),
            None,
        )
        if installed or pending:
            latest = (pending or {}).get("Version") or installed
            snapshot.add_update(
                UpdateReading(
                    key=f"{device_key}_pve_update",
                    name="Proxmox VE",
                    installed_version=(pending or {}).get("OldVersion") or installed,
                    latest_version=latest,
                    title="Proxmox VE",
                    release_url=(pending or {}).get("ChangeLogUrl"),
                    release_summary=(pending or {}).get("Title"),
                    device_key=device_key,
                    # The API exposes no way to run the upgrade; that is a
                    # deliberate Proxmox restriction, so this is read-only.
                    install=None,
                )
            )

    def _add_node_disks(self, snapshot: Snapshot, node: str, device_key: str) -> None:
        disks = self._slow.get(f"disks/{node}") or []
        for disk in disks:
            if not isinstance(disk, dict):
                continue
            devpath = disk.get("devpath")
            if not devpath:
                continue
            short = str(devpath).rsplit("/", 1)[-1]
            health = str(disk.get("health") or "UNKNOWN").upper()
            failing = health != "UNKNOWN" and health not in _HEALTHY_DISK
            snapshot.add_binary(
                BinaryReading(
                    key=f"{device_key}_disk_{short}_health",
                    name=f"Disk {short} health",
                    # UNKNOWN (e.g. a disk behind a RAID controller) is not a
                    # problem report, so it stays unknown rather than failing.
                    value=None if health == "UNKNOWN" else health not in _HEALTHY_DISK,
                    reason=(
                        f"SMART reports {disk.get('health')}"
                        f" for {disk.get('model') or short}"
                        if failing
                        else None
                    ),
                    device_class=BinarySensorDeviceClass.PROBLEM,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    device_key=device_key,
                    attributes={
                        "health": disk.get("health"),
                        "model": disk.get("model"),
                        "serial": disk.get("serial"),
                        "size_bytes": to_int(disk.get("size")),
                        "type": disk.get("type"),
                        "used": disk.get("used"),
                    },
                )
            )
            wearout = to_float(disk.get("wearout"))
            if wearout is not None:
                snapshot.add(
                    Reading(
                        key=f"{device_key}_disk_{short}_wearout",
                        name=f"Disk {short} life remaining",
                        value=wearout,
                        unit=PERCENTAGE,
                        state_class=SensorStateClass.MEASUREMENT,
                        icon="mdi:harddisk",
                        entity_category=EntityCategory.DIAGNOSTIC,
                        device_key=device_key,
                    )
                )

    def _add_node_zfs(self, snapshot: Snapshot, node: str, device_key: str) -> None:
        for pool in self._slow.get(f"zfs/{node}") or []:
            if not isinstance(pool, dict) or not pool.get("name"):
                continue
            name = str(pool["name"])
            health = str(pool.get("health") or "").upper()
            degraded = bool(health) and health not in _HEALTHY_ZFS
            snapshot.add_binary(
                BinaryReading(
                    key=f"{device_key}_zfs_{name}_health",
                    name=f"ZFS {name} health",
                    value=None if not health else health not in _HEALTHY_ZFS,
                    reason=f"Pool {name} is {pool.get('health')}" if degraded else None,
                    device_class=BinarySensorDeviceClass.PROBLEM,
                    device_key=device_key,
                    attributes={
                        "health": pool.get("health"),
                        "size_bytes": to_int(pool.get("size")),
                        "free_bytes": to_int(pool.get("free")),
                        "dedup": to_float(pool.get("dedup")),
                    },
                )
            )
            size = to_float(pool.get("size"))
            free = to_float(pool.get("free"))
            snapshot.add(
                Reading(
                    key=f"{device_key}_zfs_{name}_used",
                    name=f"ZFS {name} used",
                    value=None if None in (size, free) else percent(size - free, size),
                    unit=PERCENTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=1,
                    icon="mdi:database",
                    device_key=device_key,
                )
            )
            frag = to_float(pool.get("frag"))
            if frag is not None:
                snapshot.add(
                    Reading(
                        key=f"{device_key}_zfs_{name}_fragmentation",
                        name=f"ZFS {name} fragmentation",
                        value=frag,
                        unit=PERCENTAGE,
                        state_class=SensorStateClass.MEASUREMENT,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        device_key=device_key,
                        enabled_default=False,
                    )
                )

    def _add_node_certificates(
        self, snapshot: Snapshot, node: str, device_key: str
    ) -> None:
        certs = self._slow.get(f"certs/{node}") or []
        cert = next(
            (
                c
                for c in certs
                if isinstance(c, dict) and c.get("filename") == "pveproxy-ssl.pem"
            ),
            None,
        ) or next((c for c in certs if isinstance(c, dict)), None)
        if not cert:
            return
        snapshot.add(
            Reading(
                key=f"{device_key}_certificate_expiry",
                name="Certificate expiry",
                value=_epoch(cert.get("notafter")),
                device_class=SensorDeviceClass.TIMESTAMP,
                entity_category=EntityCategory.DIAGNOSTIC,
                device_key=device_key,
                attributes={
                    "issuer": cert.get("issuer"),
                    "subject": cert.get("subject"),
                    "fingerprint": cert.get("fingerprint"),
                },
            )
        )

    def _add_node_backup_jobs(
        self, snapshot: Snapshot, node: str, device_key: str
    ) -> None:
        tasks = [t for t in self._slow.get(f"tasks/{node}") or [] if isinstance(t, dict)]
        finished = [t for t in tasks if t.get("endtime")]
        if not finished:
            return
        latest = max(finished, key=lambda t: to_float(t.get("endtime")) or 0)
        status = str(latest.get("status") or "")
        snapshot.add(
            Reading(
                key=f"{device_key}_last_backup_job",
                name="Last backup job",
                value=_epoch(latest.get("endtime")),
                device_class=SensorDeviceClass.TIMESTAMP,
                device_key=device_key,
                attributes={
                    "status": status,
                    "started": _epoch(latest.get("starttime")),
                    "user": latest.get("user"),
                    "upid": latest.get("upid"),
                },
            )
        )
        snapshot.add_binary(
            BinaryReading(
                key=f"{device_key}_last_backup_failed",
                name="Last backup job failed",
                value=status != "OK",
                # vzdump puts its actual complaint in the task exit status.
                reason=f"vzdump exited with: {status}" if status != "OK" else None,
                device_class=BinarySensorDeviceClass.PROBLEM,
                device_key=device_key,
                attributes={"status": status},
            )
        )

    def _add_storages(
        self, snapshot: Snapshot, resources: list[dict[str, Any]]
    ) -> None:
        seen: set[str] = set()
        for entry in resources:
            if not isinstance(entry, dict) or entry.get("type") != "storage":
                continue
            name = entry.get("storage")
            if not name:
                continue
            shared = to_int(entry.get("shared")) == 1
            # A shared storage appears once per node; keep a single entity.
            unique = str(name) if shared else f"{entry.get('node')}_{name}"
            if unique in seen:
                continue
            seen.add(unique)

            label = str(name) if shared else f"{name} on {entry.get('node')}"
            snapshot.add(
                Reading(
                    key=f"storage_{unique}_used",
                    name=f"Storage {label} used",
                    value=percent(entry.get("disk"), entry.get("maxdisk")),
                    unit=PERCENTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=1,
                    icon="mdi:database",
                    attributes={
                        "used_bytes": to_int(entry.get("disk")),
                        "total_bytes": to_int(entry.get("maxdisk")),
                        "type": entry.get("plugintype"),
                        "shared": shared,
                        "node": entry.get("node"),
                    },
                )
            )
            snapshot.add(
                Reading(
                    key=f"storage_{unique}_free",
                    name=f"Storage {label} free",
                    value=(
                        None
                        if None in (to_float(entry.get("maxdisk")), to_float(entry.get("disk")))
                        else to_float(entry.get("maxdisk")) - to_float(entry.get("disk"))
                    ),
                    device_class=SensorDeviceClass.DATA_SIZE,
                    unit=UnitOfInformation.BYTES,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=0,
                    enabled_default=False,
                )
            )
            snapshot.add_binary(
                BinaryReading(
                    key=f"storage_{unique}_available",
                    name=f"Storage {label} available",
                    value=str(entry.get("status") or "").lower() == "available",
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                    entity_category=EntityCategory.DIAGNOSTIC,
                )
            )

    def _add_guests(self, snapshot: Snapshot, resources: list[dict[str, Any]]) -> None:
        backups = self._slow.get("backups") or {}
        for entry in resources:
            if not isinstance(entry, dict) or entry.get("type") not in _GUEST_TYPES:
                continue
            if to_int(entry.get("template")) == 1:
                continue
            vmid = to_int(entry.get("vmid"))
            node = entry.get("node")
            if vmid is None or not node:
                continue

            kind = entry["type"]
            name = entry.get("name") or f"{kind}/{vmid}"
            device_key = f"guest_{vmid}"
            running = str(entry.get("status") or "").lower() == "running"

            snapshot.add_device(
                DeviceMeta(
                    key=device_key,
                    name=f"{name} ({vmid})",
                    manufacturer="Proxmox",
                    model="QEMU virtual machine" if kind == "qemu" else "LXC container",
                    configuration_url=(
                        f"{self.base_url}/#v1:0:={kind}%2F{vmid}"
                    ),
                    via_device=self._node_device_key(str(node)),
                )
            )
            snapshot.add_binary(
                BinaryReading(
                    key=f"{device_key}_running",
                    name="Running",
                    value=running,
                    device_class=BinarySensorDeviceClass.RUNNING,
                    device_key=device_key,
                    attributes={
                        "vmid": vmid,
                        "node": node,
                        "type": kind,
                        "ha_state": entry.get("hastate"),
                        "tags": entry.get("tags"),
                        "lock": entry.get("lock"),
                    },
                )
            )

            base = f"/nodes/{quote(str(node), safe='')}/{kind}/{vmid}/status"
            snapshot.add_switch(
                SwitchSpec(
                    key=f"{device_key}_power",
                    name="Power",
                    value=running,
                    turn=partial(self._set_guest_power, base),
                    device_class=SwitchDeviceClass.SWITCH,
                    icon="mdi:server",
                    device_key=device_key,
                    # Start/shutdown are queued as PVE tasks, so the reported
                    # state lags the command until the next poll.
                    assumed_state=True,
                )
            )
            snapshot.add_button(
                ButtonSpec(
                    key=f"{device_key}_reboot",
                    name="Reboot",
                    press=partial(self._post, f"{base}/reboot"),
                    device_class=ButtonDeviceClass.RESTART,
                    device_key=device_key,
                    enabled_default=False,
                )
            )
            snapshot.add_button(
                ButtonSpec(
                    key=f"{device_key}_force_stop",
                    name="Force stop",
                    press=partial(self._post, f"{base}/stop"),
                    icon="mdi:stop-circle-outline",
                    device_key=device_key,
                    enabled_default=False,
                )
            )

            cpu = to_float(entry.get("cpu"))
            snapshot.add(
                Reading(
                    key=f"{device_key}_cpu",
                    name="CPU used",
                    value=None if cpu is None else round(cpu * 100, 2),
                    unit=PERCENTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=1,
                    icon="mdi:cpu-64-bit",
                    device_key=device_key,
                    attributes={"assigned_cores": to_int(entry.get("maxcpu"))},
                )
            )
            snapshot.add(
                Reading(
                    key=f"{device_key}_memory",
                    name="Memory used",
                    value=percent(entry.get("mem"), entry.get("maxmem")),
                    unit=PERCENTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=1,
                    icon="mdi:memory",
                    device_key=device_key,
                    attributes={
                        "used_bytes": to_int(entry.get("mem")),
                        "total_bytes": to_int(entry.get("maxmem")),
                    },
                )
            )
            snapshot.add(
                Reading(
                    key=f"{device_key}_disk",
                    name="Disk used",
                    value=percent(entry.get("disk"), entry.get("maxdisk")),
                    unit=PERCENTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=1,
                    icon="mdi:harddisk",
                    device_key=device_key,
                    # QEMU guests report disk=0 unless the guest agent is
                    # running, so this is off by default to avoid confusion.
                    enabled_default=kind == "lxc",
                )
            )
            snapshot.add(
                Reading(
                    key=f"{device_key}_last_boot",
                    name="Last boot",
                    value=(
                        self.boot_time(device_key, to_float(entry.get("uptime")))
                        if running
                        else None
                    ),
                    device_class=SensorDeviceClass.TIMESTAMP,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    device_key=device_key,
                )
            )

            if self._monitor_backups:
                record = backups.get(vmid)
                snapshot.add(
                    Reading(
                        key=f"{device_key}_last_backup",
                        name="Last backup",
                        value=_epoch((record or {}).get("ctime")),
                        device_class=SensorDeviceClass.TIMESTAMP,
                        device_key=device_key,
                        attributes={
                            "backup_count": (record or {}).get("count", 0),
                            "size_bytes": (record or {}).get("size"),
                            "storage": (record or {}).get("storage"),
                            "volid": (record or {}).get("volid"),
                            "protected": (record or {}).get("protected"),
                            "verification": (record or {}).get("verification"),
                        },
                    )
                )

    async def _set_guest_power(self, status_path: str, on: bool) -> None:
        await self._post(f"{status_path}/{'start' if on else 'shutdown'}")

    def _add_backup_summary(
        self, snapshot: Snapshot, resources: list[dict[str, Any]]
    ) -> None:
        backups = self._slow.get("backups")
        if backups is None:
            return
        guests = [
            r
            for r in resources
            if isinstance(r, dict)
            and r.get("type") in _GUEST_TYPES
            and to_int(r.get("template")) != 1
        ]
        never = [
            f"{r.get('name') or r.get('vmid')} ({r.get('vmid')})"
            for r in guests
            if to_int(r.get("vmid")) not in backups
        ]
        snapshot.add(
            Reading(
                key="guests_without_backup",
                name="Guests without a backup",
                value=len(never),
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:backup-restore",
                attributes={"guests": never[:100]},
            )
        )
        oldest = min(
            (rec["ctime"] for rec in backups.values() if rec.get("ctime")), default=None
        )
        if oldest:
            snapshot.add(
                Reading(
                    key="oldest_backup",
                    name="Oldest latest-backup",
                    value=_epoch(oldest),
                    device_class=SensorDeviceClass.TIMESTAMP,
                    icon="mdi:clock-alert-outline",
                    entity_category=EntityCategory.DIAGNOSTIC,
                )
            )
