"""RouterOS v7 REST backend.

Talks to the `/rest` tree that RouterOS 7 exposes on the `www-ssl` (or `www`)
service.  Everything here is plain JSON over HTTPS with HTTP Basic auth, so no
RouterOS client library is required.
"""

from __future__ import annotations

import logging
import re
from functools import partial
from typing import Any

import aiohttp


from ..const import (
    BinarySensorDeviceClass,
    ButtonDeviceClass,
    EntityCategory,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    SensorDeviceClass,
    SensorStateClass,
    SwitchDeviceClass,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTemperature,
    slugify,
)
from ..const import MAIN
from ..models import (
    BinaryReading,
    ButtonSpec,
    DeviceMeta,
    Reading,
    Snapshot,
    SwitchSpec,
    UpdateReading,
)
from .base import BaseClient, ConnectionFailed, percent, to_float, to_int, truthy

_LOGGER = logging.getLogger(__name__)

# "6w4d17:20:41", "3d10:11:12", "17:20:41" and the "1h2m3s" spelling.
_SPAN_COMPACT = re.compile(
    r"(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?"
    r"(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$"
)
_SPAN_CLOCK = re.compile(
    r"(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?(?P<h>\d+):(?P<m>\d+):(?P<s>\d+)$"
)

# RouterOS health entries carry a `type`; map it onto HA sensor semantics.
_HEALTH_TYPES: dict[str, tuple[SensorDeviceClass | None, str | None]] = {
    "C": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "V": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    "A": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    "W": (SensorDeviceClass.POWER, UnitOfPower.WATT),
    "RPM": (None, REVOLUTIONS_PER_MINUTE),
}

# Interfaces whose byte counters are rarely interesting on their own.
_DULL_INTERFACE_TYPES = {"loopback"}

# WireGuard is connectionless: a peer has no "up" flag, only the time of its
# last handshake. Peers rekey about every two minutes, so silence for much
# longer than that means the tunnel is not passing traffic.
_WIREGUARD_STALE_AFTER = 180.0

# Dial-out tunnel interfaces, which all carry the same running/disabled shape.
_TUNNEL_CLIENTS = (
    ("/interface/ovpn-client", "OpenVPN", "ovpn"),
    ("/interface/l2tp-client", "L2TP", "l2tp"),
    ("/interface/sstp-client", "SSTP", "sstp"),
    ("/interface/pptp-client", "PPTP", "pptp"),
)

# IPsec phase 2 is only carrying traffic once it reaches this state.
_IPSEC_ESTABLISHED = "established"


def parse_timespan(value: Any) -> float | None:
    """Parse a RouterOS timespan into seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _SPAN_CLOCK.match(text) or _SPAN_COMPACT.match(text)
    if not match or not any(match.groupdict().values()):
        return None
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return float(
        parts["w"] * 604800
        + parts["d"] * 86400
        + parts["h"] * 3600
        + parts["m"] * 60
        + parts["s"]
    )


class MikrotikClient(BaseClient):
    """RouterOS REST client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_ssl: bool = True,
        monitor_interfaces: bool = True,
        monitor_tunnels: bool = True,
        monitor_netwatch: bool = True,
    ) -> None:
        super().__init__(session, host, port, use_ssl=use_ssl)
        self._auth = aiohttp.BasicAuth(username, password)
        self._monitor_interfaces = monitor_interfaces
        self._monitor_tunnels = monitor_tunnels
        self._monitor_netwatch = monitor_netwatch
        # `check-for-updates` makes the router perform an outbound HTTP call, so
        # it only runs on the slow tier rather than on every poll.
        self._update_checked = False

    async def _get(self, path: str, *, soft: bool = False) -> Any:
        """GET a REST path.  `soft` tolerates endpoints missing on this device."""
        return await self._request(
            "GET",
            f"{self.base_url}/rest{path}",
            auth=self._auth,
            allow_status=(400, 404, 405, 500) if soft else (),
        )

    async def _post(self, path: str, payload: Any = None) -> Any:
        return await self._request(
            "POST", f"{self.base_url}/rest{path}", auth=self._auth, json=payload or {}
        )

    async def _patch(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._request(
            "PATCH", f"{self.base_url}/rest{path}", auth=self._auth, json=payload
        )

    async def async_validate(self) -> dict[str, Any]:
        resource = await self._get("/system/resource")
        if not isinstance(resource, dict):
            raise ConnectionFailed(
                "Unexpected response from /rest/system/resource — is this "
                "RouterOS v7 with the 'www-ssl' service enabled?"
            )
        identity = await self._get("/system/identity", soft=True) or {}
        board = await self._get("/system/routerboard", soft=True) or {}
        serial = board.get("serial-number")
        return {
            "unique_id": serial or f"{self._host}:{self._port}",
            "title": identity.get("name") or resource.get("board-name") or self._host,
            "model": resource.get("board-name"),
        }

    async def async_fetch(self, *, slow: bool) -> Snapshot:
        snapshot = Snapshot()

        resource = await self._get("/system/resource") or {}
        identity = await self._get("/system/identity", soft=True) or {}
        board = await self._get("/system/routerboard", soft=True) or {}

        name = identity.get("name") or resource.get("board-name") or self._host
        snapshot.add_device(
            DeviceMeta(
                key=MAIN,
                name=name,
                manufacturer="MikroTik",
                model=board.get("model") or resource.get("board-name"),
                sw_version=resource.get("version"),
                hw_version=board.get("current-firmware"),
                serial_number=board.get("serial-number"),
                configuration_url=self.base_url,
            )
        )

        self._add_resource(snapshot, resource)
        await self._add_health(snapshot)
        await self._add_updates(snapshot, board, slow=slow)
        await self._add_connections(snapshot)
        if self._monitor_interfaces:
            await self._add_interfaces(snapshot)
        if self._monitor_tunnels:
            await self._add_tunnels(snapshot)
        if self._monitor_netwatch:
            await self._add_netwatch(snapshot)
        await self._add_poe(snapshot)

        snapshot.add_button(
            ButtonSpec(
                key="reboot",
                name="Reboot",
                press=partial(self._post, "/system/reboot"),
                device_class=ButtonDeviceClass.RESTART,
                entity_category=EntityCategory.CONFIG,
                enabled_default=False,
            )
        )
        return snapshot

    # -- sections ---------------------------------------------------------

    def _add_resource(self, snapshot: Snapshot, resource: dict[str, Any]) -> None:
        free_mem = to_float(resource.get("free-memory"))
        total_mem = to_float(resource.get("total-memory"))
        free_hdd = to_float(resource.get("free-hdd-space"))
        total_hdd = to_float(resource.get("total-hdd-space"))
        used_mem = None if None in (free_mem, total_mem) else total_mem - free_mem
        used_hdd = None if None in (free_hdd, total_hdd) else total_hdd - free_hdd

        snapshot.add(
            Reading(
                key="cpu_load",
                name="CPU load",
                value=to_float(resource.get("cpu-load")),
                unit=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:cpu-32-bit",
            )
        )
        snapshot.add(
            Reading(
                key="memory_used_percent",
                name="Memory used",
                value=percent(used_mem, total_mem),
                unit=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=1,
                icon="mdi:memory",
            )
        )
        snapshot.add(
            Reading(
                key="memory_free",
                name="Memory free",
                value=free_mem,
                device_class=SensorDeviceClass.DATA_SIZE,
                unit=UnitOfInformation.BYTES,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=0,
                enabled_default=False,
            )
        )
        snapshot.add(
            Reading(
                key="disk_used_percent",
                name="Disk used",
                value=percent(used_hdd, total_hdd),
                unit=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=1,
                icon="mdi:harddisk",
            )
        )
        snapshot.add(
            Reading(
                key="last_boot",
                name="Last boot",
                value=self.boot_time(MAIN, parse_timespan(resource.get("uptime"))),
                device_class=SensorDeviceClass.TIMESTAMP,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        )
        snapshot.add(
            Reading(
                key="version",
                name="RouterOS version",
                value=resource.get("version"),
                entity_category=EntityCategory.DIAGNOSTIC,
                icon="mdi:package-variant",
                attributes={
                    "architecture": resource.get("architecture-name"),
                    "board_name": resource.get("board-name"),
                    "platform": resource.get("platform"),
                    "cpu": resource.get("cpu"),
                    "cpu_count": to_int(resource.get("cpu-count")),
                    "cpu_frequency_mhz": to_int(resource.get("cpu-frequency")),
                    "build_time": resource.get("build-time"),
                },
            )
        )
        bad_blocks = to_float(resource.get("bad-blocks"))
        if bad_blocks is not None:
            snapshot.add(
                Reading(
                    key="bad_blocks",
                    name="Storage bad blocks",
                    value=bad_blocks,
                    unit=PERCENTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    icon="mdi:harddisk-remove",
                )
            )

    async def _add_health(self, snapshot: Snapshot) -> None:
        """Expose /system/health, whose shape differs between RouterOS builds."""
        health = await self._get("/system/health", soft=True)
        entries: list[dict[str, Any]] = []
        if isinstance(health, list):
            entries = [e for e in health if isinstance(e, dict)]
        elif isinstance(health, dict):
            # Older builds return a flat object rather than a list of readings.
            entries = [
                {"name": k, "value": v}
                for k, v in health.items()
                if not k.startswith(".")
            ]

        for entry in entries:
            raw_name = entry.get("name")
            if not raw_name:
                continue
            key = f"health_{slugify(raw_name)}"
            name = str(raw_name).replace("-", " ").capitalize()
            value = entry.get("value")

            if "state" in raw_name:
                # e.g. psu1-state = ok / fail
                ok = truthy(value)
                snapshot.add_binary(
                    BinaryReading(
                        key=key,
                        name=name,
                        value=None if ok is None else not ok,
                        # RouterOS reports the raw state word ("fail"), which
                        # is more use than a bare "problem".
                        reason=f"{name} reports {value}" if ok is False else None,
                        device_class=BinarySensorDeviceClass.PROBLEM,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        attributes={"raw_state": value},
                    )
                )
                continue

            unit_type = str(entry.get("type") or "").upper()
            device_class, unit = _HEALTH_TYPES.get(unit_type, (None, None))
            if device_class is None and unit is None:
                # No type field: infer from the reading name.
                lowered = str(raw_name).lower()
                if "temp" in lowered:
                    device_class, unit = (
                        SensorDeviceClass.TEMPERATURE,
                        UnitOfTemperature.CELSIUS,
                    )
                elif "fan" in lowered or "speed" in lowered:
                    unit = REVOLUTIONS_PER_MINUTE
                elif "voltage" in lowered or lowered.endswith("v"):
                    device_class, unit = (
                        SensorDeviceClass.VOLTAGE,
                        UnitOfElectricPotential.VOLT,
                    )
                elif "current" in lowered:
                    device_class, unit = (
                        SensorDeviceClass.CURRENT,
                        UnitOfElectricCurrent.AMPERE,
                    )
                elif "power" in lowered:
                    device_class, unit = SensorDeviceClass.POWER, UnitOfPower.WATT

            numeric = to_float(value)
            if numeric is None:
                continue
            snapshot.add(
                Reading(
                    key=key,
                    name=name,
                    value=numeric,
                    device_class=device_class,
                    unit=unit,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    icon=None if device_class else "mdi:fan",
                )
            )

    async def _check_for_updates(self) -> None:
        await self._post("/system/package/update/check-for-updates")
        self._update_checked = True

    async def _add_updates(
        self, snapshot: Snapshot, board: dict[str, Any], *, slow: bool
    ) -> None:
        if slow or not self._update_checked:
            try:
                await self._check_for_updates()
            except ConnectionFailed as err:
                # No internet on the router is not a reason to fail the poll.
                _LOGGER.debug("RouterOS update check failed on %s: %s", self._host, err)

        snapshot.add_button(
            ButtonSpec(
                key="check_for_updates",
                name="Check for updates",
                # The scheduled check only runs on the slow tier, because it
                # makes the router call out to MikroTik; this forces one.
                press=self._check_for_updates,
                icon="mdi:cloud-download-outline",
                entity_category=EntityCategory.CONFIG,
            )
        )

        state = await self._get("/system/package/update", soft=True) or {}
        installed = state.get("installed-version")
        latest = state.get("latest-version")
        if installed:
            snapshot.add_update(
                UpdateReading(
                    key="routeros_update",
                    name="RouterOS",
                    installed_version=installed,
                    latest_version=latest or installed,
                    title="RouterOS",
                    release_url="https://mikrotik.com/download/changelogs",
                    install=self._install_routeros,
                    attributes={
                        "channel": state.get("channel"),
                        "status": state.get("status"),
                    },
                )
            )

        current_fw = board.get("current-firmware")
        upgrade_fw = board.get("upgrade-firmware")
        if current_fw:
            snapshot.add_update(
                UpdateReading(
                    key="routerboard_firmware",
                    name="RouterBOARD firmware",
                    installed_version=current_fw,
                    latest_version=upgrade_fw or current_fw,
                    title="RouterBOARD firmware",
                    install=self._install_firmware,
                    entity_category=EntityCategory.CONFIG,
                    attributes={"firmware_type": board.get("firmware-type")},
                )
            )

    async def _install_routeros(self) -> None:
        """Install the pending RouterOS package set.  The router reboots."""
        await self._post("/system/package/update/install")

    async def _install_firmware(self) -> None:
        """Flash the bundled RouterBOARD firmware, then reboot to apply it."""
        await self._post("/system/routerboard/upgrade")
        await self._post("/system/reboot")

    async def _add_connections(self, snapshot: Snapshot) -> None:
        tracking = await self._get("/ip/firewall/connection/tracking", soft=True)
        if not isinstance(tracking, dict):
            return
        total = to_int(tracking.get("total-entries"))
        if total is None:
            return
        snapshot.add(
            Reading(
                key="connections",
                name="Tracked connections",
                value=total,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:lan-connect",
                entity_category=EntityCategory.DIAGNOSTIC,
                attributes={"max_entries": to_int(tracking.get("max-entries"))},
            )
        )

    async def _add_interfaces(self, snapshot: Snapshot) -> None:
        interfaces = await self._get("/interface", soft=True)
        if not isinstance(interfaces, list):
            return
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            iface_name = iface.get("name")
            if not iface_name or truthy(iface.get("disabled")):
                continue
            if iface.get("type") in _DULL_INTERFACE_TYPES:
                continue
            slug = slugify(iface_name)
            snapshot.add_binary(
                BinaryReading(
                    key=f"iface_{slug}_running",
                    name=f"{iface_name} link",
                    value=truthy(iface.get("running")),
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={
                        "type": iface.get("type"),
                        "mac_address": iface.get("mac-address"),
                        "mtu": iface.get("mtu"),
                        "comment": iface.get("comment"),
                    },
                )
            )
            for direction, field_name in (("rx", "rx-byte"), ("tx", "tx-byte")):
                snapshot.add(
                    Reading(
                        key=f"iface_{slug}_{direction}_bytes",
                        name=f"{iface_name} {direction.upper()}",
                        value=to_float(iface.get(field_name)),
                        device_class=SensorDeviceClass.DATA_SIZE,
                        unit=UnitOfInformation.BYTES,
                        # TOTAL_INCREASING lets HA derive throughput and cope
                        # with the counter resetting on reboot.
                        state_class=SensorStateClass.TOTAL_INCREASING,
                        suggested_display_precision=0,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )

    # -- tunnels ----------------------------------------------------------

    async def _add_tunnels(self, snapshot: Snapshot) -> None:
        """VPN tunnels, whatever kind they are.

        Every endpoint here is `soft`: a router with no WireGuard package, or
        an older RouterOS without a given menu, must not fail the whole poll.
        """
        await self._add_wireguard(snapshot)
        await self._add_tunnel_clients(snapshot)
        await self._add_ipsec(snapshot)
        await self._add_ppp_sessions(snapshot)

    async def _add_wireguard(self, snapshot: Snapshot) -> None:
        interfaces = await self._get("/interface/wireguard", soft=True)
        if isinstance(interfaces, list):
            for iface in interfaces:
                if not isinstance(iface, dict) or not iface.get("name"):
                    continue
                if truthy(iface.get("disabled")):
                    continue
                iface_name = str(iface["name"])
                snapshot.add_binary(
                    BinaryReading(
                        key=f"wg_{slugify(iface_name)}_running",
                        name=f"WireGuard {iface_name}",
                        value=truthy(iface.get("running")),
                        device_class=BinarySensorDeviceClass.CONNECTIVITY,
                        icon="mdi:vpn",
                        attributes={
                            "listen_port": to_int(iface.get("listen-port")),
                            "public_key": iface.get("public-key"),
                            "comment": iface.get("comment"),
                        },
                    )
                )

        peers = await self._get("/interface/wireguard/peers", soft=True)
        if not isinstance(peers, list):
            return
        for peer in peers:
            if not isinstance(peer, dict) or truthy(peer.get("disabled")):
                continue
            iface_name = str(peer.get("interface") or "")
            label = (
                peer.get("name")
                or peer.get("comment")
                # Nothing else identifies a peer, and the key is unique.
                or str(peer.get("public-key") or "")[:12]
            )
            if not label:
                continue
            key = f"wg_peer_{slugify(f'{iface_name}_{label}')}"
            age = parse_timespan(peer.get("last-handshake"))
            snapshot.add_binary(
                BinaryReading(
                    key=f"{key}_connected",
                    name=f"WireGuard peer {label}",
                    # No handshake recorded at all means it has never come up,
                    # which is down rather than unknown.
                    value=None if age is None and peer.get("last-handshake") else (
                        age is not None and age <= _WIREGUARD_STALE_AFTER
                    ),
                    reason=(
                        None
                        if age is not None and age <= _WIREGUARD_STALE_AFTER
                        else (
                            f"No handshake for {int(age)}s"
                            if age is not None
                            else "No handshake recorded"
                        )
                    ),
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                    icon="mdi:vpn",
                    attributes={
                        "interface": iface_name,
                        "endpoint": peer.get("endpoint-address"),
                        "allowed_address": peer.get("allowed-address"),
                        "comment": peer.get("comment"),
                    },
                )
            )
            if age is not None:
                snapshot.add(
                    Reading(
                        key=f"{key}_handshake_age",
                        name=f"WireGuard peer {label} last handshake",
                        value=age,
                        unit="s",
                        state_class=SensorStateClass.MEASUREMENT,
                        icon="mdi:handshake",
                        entity_category=EntityCategory.DIAGNOSTIC,
                    )
                )
            for direction in ("rx", "tx"):
                total = to_float(peer.get(direction))
                if total is None:
                    continue
                snapshot.add(
                    Reading(
                        key=f"{key}_{direction}_bytes",
                        name=f"WireGuard peer {label} {direction.upper()}",
                        value=total,
                        device_class=SensorDeviceClass.DATA_SIZE,
                        unit=UnitOfInformation.BYTES,
                        state_class=SensorStateClass.TOTAL_INCREASING,
                        suggested_display_precision=0,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )

    async def _add_tunnel_clients(self, snapshot: Snapshot) -> None:
        """OpenVPN, L2TP, SSTP and PPTP clients, which share a shape."""
        for path, label, prefix in _TUNNEL_CLIENTS:
            entries = await self._get(path, soft=True)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                if truthy(entry.get("disabled")):
                    continue
                name = str(entry["name"])
                running = truthy(entry.get("running"))
                snapshot.add_binary(
                    BinaryReading(
                        key=f"{prefix}_{slugify(name)}_connected",
                        name=f"{label} {name}",
                        value=running,
                        reason=(
                            None
                            if running
                            else f"{label} client {name} is not connected"
                        ),
                        device_class=BinarySensorDeviceClass.CONNECTIVITY,
                        icon="mdi:vpn",
                        attributes={
                            "connect_to": entry.get("connect-to"),
                            "user": entry.get("user"),
                            "comment": entry.get("comment"),
                        },
                    )
                )

    async def _add_ipsec(self, snapshot: Snapshot) -> None:
        peers = await self._get("/ip/ipsec/active-peers", soft=True)
        if isinstance(peers, list):
            for peer in peers:
                if not isinstance(peer, dict):
                    continue
                remote = peer.get("remote-address") or peer.get("id")
                if not remote:
                    continue
                state = str(peer.get("state") or "").lower()
                snapshot.add_binary(
                    BinaryReading(
                        key=f"ipsec_peer_{slugify(str(remote))}_established",
                        name=f"IPsec peer {remote}",
                        value=state == _IPSEC_ESTABLISHED,
                        reason=(
                            None
                            if state == _IPSEC_ESTABLISHED
                            else f"Phase 1 is {peer.get('state') or 'not established'}"
                        ),
                        device_class=BinarySensorDeviceClass.CONNECTIVITY,
                        icon="mdi:security-network",
                        attributes={
                            "local_address": peer.get("local-address"),
                            "state": peer.get("state"),
                            "uptime": peer.get("uptime"),
                        },
                    )
                )

        policies = await self._get("/ip/ipsec/policy", soft=True)
        if not isinstance(policies, list):
            return
        for policy in policies:
            if not isinstance(policy, dict) or truthy(policy.get("disabled")):
                continue
            # RouterOS keeps a built-in template policy that never establishes.
            if truthy(policy.get("template")):
                continue
            src = policy.get("src-address")
            dst = policy.get("dst-address")
            if not dst:
                continue
            state = str(policy.get("ph2-state") or "").lower()
            snapshot.add_binary(
                BinaryReading(
                    key=f"ipsec_policy_{slugify(f'{src}_{dst}')}_installed",
                    name=f"IPsec policy {src} → {dst}",
                    value=state == _IPSEC_ESTABLISHED,
                    reason=(
                        None
                        if state == _IPSEC_ESTABLISHED
                        else f"Phase 2 is {policy.get('ph2-state') or 'not established'}"
                    ),
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                    icon="mdi:security-network",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={
                        "src_address": src,
                        "dst_address": dst,
                        "ph2_state": policy.get("ph2-state"),
                    },
                )
            )

    async def _add_ppp_sessions(self, snapshot: Snapshot) -> None:
        """Inbound tunnel sessions, counted rather than enumerated.

        Dial-in sessions come and go constantly; one entity per session would
        churn the Home Assistant entity registry for no benefit, so this
        reports a count per service with the names as an attribute.
        """
        active = await self._get("/ppp/active", soft=True)
        if not isinstance(active, list):
            return
        by_service: dict[str, list[str]] = {}
        for session in active:
            if not isinstance(session, dict):
                continue
            service = str(session.get("service") or "other").lower()
            by_service.setdefault(service, []).append(
                str(session.get("name") or session.get("caller-id") or "?")
            )

        snapshot.add(
            Reading(
                key="ppp_active_total",
                name="Tunnel sessions",
                value=sum(len(v) for v in by_service.values()),
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:account-network",
                attributes={service: names for service, names in by_service.items()},
            )
        )
        for service, names in by_service.items():
            snapshot.add(
                Reading(
                    key=f"ppp_active_{slugify(service)}",
                    name=f"{service.upper()} sessions",
                    value=len(names),
                    state_class=SensorStateClass.MEASUREMENT,
                    icon="mdi:account-network",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={"users": names},
                )
            )

    # -- netwatch ---------------------------------------------------------

    async def _add_netwatch(self, snapshot: Snapshot) -> None:
        """RouterOS's own reachability probes.

        Netwatch is where an operator has already said which hosts matter, so
        it maps straight onto connectivity entities.
        """
        entries = await self._get("/tool/netwatch", soft=True)
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict) or truthy(entry.get("disabled")):
                continue
            host = entry.get("host")
            if not host:
                continue
            label = str(entry.get("comment") or host)
            key = f"netwatch_{slugify(str(entry.get('comment') or host))}"
            status = str(entry.get("status") or "").lower()
            up = truthy(status) if status else None
            snapshot.add_binary(
                BinaryReading(
                    key=f"{key}_up",
                    name=f"Netwatch {label}",
                    value=up,
                    reason=(
                        f"{host} has been {status} since {entry.get('since')}"
                        if up is False
                        else None
                    ),
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                    icon="mdi:lan-check",
                    attributes={
                        "host": host,
                        "type": entry.get("type"),
                        "since": entry.get("since"),
                        "comment": entry.get("comment"),
                    },
                )
            )
            # RouterOS 7 reports timings for ICMP probes only.
            rtt = to_float(str(entry.get("rtt-avg") or "").removesuffix("ms") or None)
            if rtt is not None:
                snapshot.add(
                    Reading(
                        key=f"{key}_rtt",
                        name=f"Netwatch {label} latency",
                        value=rtt,
                        unit="ms",
                        state_class=SensorStateClass.MEASUREMENT,
                        icon="mdi:timer-outline",
                        suggested_display_precision=1,
                    )
                )
            loss = to_float(str(entry.get("loss-percent") or "").removesuffix("%") or None)
            if loss is not None:
                snapshot.add(
                    Reading(
                        key=f"{key}_loss",
                        name=f"Netwatch {label} packet loss",
                        value=loss,
                        unit=PERCENTAGE,
                        state_class=SensorStateClass.MEASUREMENT,
                        icon="mdi:package-variant-closed-remove",
                        entity_category=EntityCategory.DIAGNOSTIC,
                    )
                )

    async def _add_poe(self, snapshot: Snapshot) -> None:
        """Expose PoE-out ports as switches so downstream gear can be power-cycled."""
        ports = await self._get("/interface/ethernet/poe", soft=True)
        if not isinstance(ports, list):
            return
        for port in ports:
            if not isinstance(port, dict):
                continue
            port_name = port.get("name")
            port_id = port.get(".id")
            if not port_name or not port_id:
                continue
            mode = str(port.get("poe-out") or "").lower()
            snapshot.add_switch(
                SwitchSpec(
                    key=f"poe_{slugify(port_name)}",
                    name=f"{port_name} PoE out",
                    value=mode in ("auto-on", "forced-on"),
                    turn=partial(self._set_poe, port_id, mode),
                    device_class=SwitchDeviceClass.OUTLET,
                    icon="mdi:ethernet",
                    attributes={"poe_out": port.get("poe-out")},
                )
            )

    async def _set_poe(self, port_id: str, previous_mode: str, on: bool) -> None:
        # Turning back on restores "auto-on" unless the port was explicitly
        # forced, in which case that preference is preserved.
        target = ("forced-on" if previous_mode == "forced-on" else "auto-on") if on else "off"
        await self._patch(f"/interface/ethernet/poe/{port_id}", {"poe-out": target})
