"""Redfish backend for Supermicro (and other DMTF-compliant) BMCs.

Redfish is the modern out-of-band REST API that replaces raw IPMI: plain
HTTPS + JSON + HTTP Basic auth against `https://<bmc>/redfish/v1/`.  It is
present on Supermicro X11 (recent firmware) and every X12/X13/H12/H13 board,
as well as on Dell iDRAC, HPE iLO and Lenovo XCC.

Everything is discovered from the service root rather than hard-coded, because
sensor inventories differ per board, and because Supermicro moved from the
legacy `Thermal`/`Power` resources to the newer `Sensors` collection partway
through the X12 generation.  Both shapes are handled.

Chassis power control uses the standard `ComputerSystem.Reset` action, so it is
the same code path that `ipmitool chassis power on/off` drives underneath.
"""

from __future__ import annotations

import logging
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
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    slugify,
)
from ..const import MAIN, POWER_OFF_GRACEFUL
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
    to_float,
    to_int,
    truthy,
)

_LOGGER = logging.getLogger(__name__)

_OK_HEALTH = {"OK"}

# Where sessions are created when the service root does not say.
DEFAULT_SESSIONS_PATH = "/redfish/v1/SessionService/Sessions"

# Reset actions offered as buttons, in the order they should appear.
_RESET_BUTTONS: list[tuple[str, str, str | None, ButtonDeviceClass | None]] = [
    ("On", "Power on", "mdi:power-plug", None),
    ("GracefulShutdown", "Graceful shutdown", "mdi:power", None),
    ("ForceOff", "Force off", "mdi:power-plug-off", None),
    ("GracefulRestart", "Graceful restart", None, ButtonDeviceClass.RESTART),
    ("ForceRestart", "Force restart", None, ButtonDeviceClass.RESTART),
    ("PowerCycle", "Power cycle", "mdi:restart-alert", None),
    ("Nmi", "Diagnostic interrupt (NMI)", "mdi:alert-octagon", None),
]

# Redfish `ReadingType` / `ReadingUnits` -> HA sensor semantics.
_READING_TYPES: dict[str, tuple[SensorDeviceClass | None, str | None]] = {
    "temperature": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "voltage": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    "current": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    "power": (SensorDeviceClass.POWER, UnitOfPower.WATT),
    "energyjoules": (SensorDeviceClass.ENERGY, None),
    "rotational": (None, REVOLUTIONS_PER_MINUTE),
    "rpm": (None, REVOLUTIONS_PER_MINUTE),
    "percent": (None, PERCENTAGE),
    "frequency": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
}


def _health_reason(status: dict[str, Any], health: Any, state: str) -> str:
    """Say what is actually wrong, in the firmware's own words where possible.

    `Status.Conditions` is where a BMC puts the specific complaint ("Power
    Supply 2 has failed"); the health rollup on its own only says that
    *something* is unhappy.
    """
    messages = [
        str(condition.get("Message") or condition.get("MessageId"))
        for condition in status.get("Conditions") or []
        if isinstance(condition, dict)
        and (condition.get("Message") or condition.get("MessageId"))
    ]
    if messages:
        return "; ".join(messages[:3])

    text = f"Health is {health}"
    if state and state.lower() != "enabled":
        text = f"{text} (state {state})"
    return text


def _health_problem(status: Any) -> tuple[bool | None, str | None]:
    """Turn a Redfish `Status` object into a PROBLEM state and its explanation."""
    if not isinstance(status, dict):
        return None, None
    state = str(status.get("State") or "")
    if state.lower() in ("absent", "disabled"):
        return None, None
    health = status.get("Health") or status.get("HealthRollup")
    if health is None:
        return None, None
    if str(health).upper() in _OK_HEALTH:
        return False, None
    return True, _health_reason(status, health, state)


class RedfishClient(BaseClient):
    """Redfish client driven entirely by service-root discovery."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        power_off_action: str = POWER_OFF_GRACEFUL,
    ) -> None:
        super().__init__(session, host, port, use_ssl=True)
        self._username = username
        self._password = password
        self._auth = aiohttp.BasicAuth(username, password)
        self._power_off_action = power_off_action
        self._slow: dict[str, Any] = {}

        # Some firmware serves Redfish but refuses HTTP Basic, requiring a
        # session token instead. Basic is tried first because it needs no
        # server-side state; on rejection this switches to session auth for
        # the client's lifetime.
        self._use_session_auth = False
        self._token: str | None = None
        self._session_uri: str | None = None
        self._sessions_path = DEFAULT_SESSIONS_PATH

    # -- authentication ---------------------------------------------------

    def _remember_sessions_path(self, root: dict[str, Any]) -> None:
        """Note where sessions are created, as advertised by the service root."""
        links = root.get("Links") or {}
        path = self._odata(links, "Sessions") or self._odata(
            root.get("SessionService") or {}, "Sessions"
        )
        if not path and (service := self._odata(root, "SessionService")):
            path = f"{service.rstrip('/')}/Sessions"
        if path:
            self._sessions_path = path

    async def _async_open_session(self) -> None:
        """Exchange the credentials for an X-Auth-Token.

        A single session is created and reused. Supermicro BMCs cap concurrent
        sessions and are slow to expire them, so creating one per request would
        eventually lock the account out of its own API.
        """
        url = f"{self.base_url}{self._sessions_path}"
        try:
            # Via _send, so a firmware that 301s this URI to its trailing-slash
            # form still receives the POST body rather than a bodyless GET.
            response = await self._send(
                "POST",
                url,
                headers=None,
                auth=None,
                json={"UserName": self._username, "Password": self._password},
                data=None,
                timeout=self._timeout,
            )
        except aiohttp.ClientError as err:
            raise ConnectionFailed(f"Error opening a Redfish session: {err}") from err
        except TimeoutError as err:
            raise ConnectionFailed("Timeout opening a Redfish session") from err

        async with response:
            token = response.headers.get("X-Auth-Token")
            if response.status >= 400 or not token:
                body = (await response.text())[:300]
                raise AuthenticationError(
                    f"Redfish rejected both HTTP Basic and session login at "
                    f"{url} (HTTP {response.status}): {body}"
                )
            self._token = token
            self._session_uri = response.headers.get("Location") or None
            self._use_session_auth = True
            _LOGGER.debug("Opened a Redfish session on %s", self._host)

    async def async_close(self) -> None:
        """Release the Redfish session so it does not linger on the BMC."""
        if not self._session_uri or not self._token:
            return
        uri = self._session_uri
        url = uri if uri.startswith("http") else f"{self.base_url}{uri}"
        try:
            response = await self._send(
                "DELETE",
                url,
                headers={"X-Auth-Token": self._token},
                auth=None,
                json=None,
                data=None,
                timeout=self._timeout,
            )
            async with response:
                pass
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Could not close the Redfish session: %s", err)
        finally:
            self._token = None
            self._session_uri = None

    async def _call(
        self,
        method: str,
        url: str,
        *,
        allow_status: tuple[int, ...] = (),
        json: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> Any:
        """Issue a request with whichever authentication this BMC accepts."""
        if self._use_session_auth:
            headers = {"X-Auth-Token": self._token} if self._token else None
            auth = None
        else:
            headers, auth = None, self._auth

        try:
            return await self._request(
                method, url, headers=headers, auth=auth, json=json,
                allow_status=allow_status,
            )
        except AuthenticationError:
            if not retry:
                raise
            # Either Basic is not accepted here, or the session token expired.
            # Both are fixed by opening a fresh session and trying once more.
            self._token = None
            await self._async_open_session()
            return await self._call(
                method, url, allow_status=allow_status, json=json, retry=False
            )

    async def _get(self, path: str, *, soft: bool = True) -> Any:
        if not path:
            return None
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        return await self._call(
            "GET",
            url,
            # BMCs return assorted errors for resources they do not implement;
            # a missing subsystem must not fail the whole poll.
            allow_status=(400, 404, 405, 500, 501) if soft else (),
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._call("POST", f"{self.base_url}{path}", json=payload)

    async def _patch(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._call("PATCH", f"{self.base_url}{path}", json=payload)

    @staticmethod
    def _odata(obj: Any, key: str) -> str | None:
        """Pull a link out of a Redfish resource."""
        value = (obj or {}).get(key)
        if isinstance(value, dict):
            return value.get("@odata.id")
        if isinstance(value, str):
            return value
        return None

    async def _first_member(self, collection_path: str | None) -> dict[str, Any] | None:
        """Fetch the first member of a Redfish collection."""
        if not collection_path:
            return None
        collection = await self._get(collection_path)
        members = (collection or {}).get("Members") or []
        for member in members:
            path = member.get("@odata.id") if isinstance(member, dict) else None
            resource = await self._get(path)
            if isinstance(resource, dict):
                return resource
        return None

    # -- config flow ------------------------------------------------------

    async def async_validate(self) -> dict[str, Any]:
        root = await self._get("/redfish/v1/", soft=False)
        if isinstance(root, dict):
            self._remember_sessions_path(root)
        if not isinstance(root, dict) or "Systems" not in root:
            raise ConnectionFailed(
                "No Redfish service found at /redfish/v1/. Older Supermicro "
                "boards (X9/X10) do not implement Redfish."
            )
        system = await self._first_member(self._odata(root, "Systems")) or {}
        serial = system.get("SerialNumber") or root.get("UUID")
        model = system.get("Model") or root.get("Product")
        return {
            "unique_id": (serial or f"{self._host}:{self._port}").strip(),
            "title": (model or self._host).strip(),
            "model": model,
        }

    # -- polling ----------------------------------------------------------

    async def async_fetch(self, *, slow: bool) -> Snapshot:
        snapshot = Snapshot()

        root = await self._get("/redfish/v1/", soft=False)
        if not isinstance(root, dict):
            raise ConnectionFailed("Empty Redfish service root")
        # Learn where to log in before the first authenticated call needs it.
        self._remember_sessions_path(root)

        system = await self._first_member(self._odata(root, "Systems")) or {}
        chassis = await self._first_member(self._odata(root, "Chassis")) or {}
        manager = await self._first_member(self._odata(root, "Managers")) or {}

        if slow or not self._slow:
            self._slow["firmware"] = await self._collect_firmware(root)
            self._slow["log"] = await self._collect_log(system)

        self._add_device(snapshot, system, chassis, manager, root)
        self._add_system(snapshot, system)
        self._add_power_control(snapshot, system, manager)
        await self._add_thermal_and_power(snapshot, chassis)
        self._add_manager(snapshot, manager)
        self._add_firmware_inventory(snapshot)
        self._add_indicator(snapshot, chassis)
        self._add_log(snapshot)
        return snapshot

    def _add_device(
        self,
        snapshot: Snapshot,
        system: dict[str, Any],
        chassis: dict[str, Any],
        manager: dict[str, Any],
        root: dict[str, Any],
    ) -> None:
        model = system.get("Model") or chassis.get("Model") or "Redfish host"
        snapshot.add_device(
            DeviceMeta(
                key=MAIN,
                name=str(system.get("HostName") or model or self._host).strip(),
                manufacturer=(
                    system.get("Manufacturer") or chassis.get("Manufacturer")
                ),
                model=str(model).strip(),
                sw_version=system.get("BiosVersion"),
                hw_version=manager.get("FirmwareVersion"),
                serial_number=system.get("SerialNumber") or chassis.get("SerialNumber"),
                configuration_url=self.base_url,
            )
        )
        snapshot.add(
            Reading(
                key="redfish_version",
                name="Redfish version",
                value=root.get("RedfishVersion"),
                entity_category=EntityCategory.DIAGNOSTIC,
                icon="mdi:api",
                enabled_default=False,
            )
        )

    def _add_system(self, snapshot: Snapshot, system: dict[str, Any]) -> None:
        if not system:
            return
        power_state = system.get("PowerState")
        snapshot.add(
            Reading(
                key="power_state",
                name="Power state",
                value=str(power_state) if power_state else None,
                device_class=SensorDeviceClass.ENUM,
                options=["On", "Off", "PoweringOn", "PoweringOff", "Paused", "Unknown"],
                icon="mdi:power",
            )
        )
        problem, reason = _health_problem(system.get("Status"))
        snapshot.add_binary(
            BinaryReading(
                key="system_health",
                name="System health",
                value=problem,
                reason=reason,
                device_class=BinarySensorDeviceClass.PROBLEM,
                attributes={
                    "state": (system.get("Status") or {}).get("State"),
                    "health": (system.get("Status") or {}).get("Health"),
                },
            )
        )

        memory = system.get("MemorySummary") or {}
        if memory:
            problem, reason = _health_problem(memory.get("Status"))
            snapshot.add_binary(
                BinaryReading(
                    key="memory_health",
                    name="Memory health",
                    value=problem,
                    reason=reason,
                    device_class=BinarySensorDeviceClass.PROBLEM,
                    entity_category=EntityCategory.DIAGNOSTIC,
                )
            )
            total = to_float(memory.get("TotalSystemMemoryGiB"))
            if total is not None:
                snapshot.add(
                    Reading(
                        key="memory_total",
                        name="Installed memory",
                        value=total,
                        unit="GiB",
                        icon="mdi:memory",
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )

        processors = system.get("ProcessorSummary") or {}
        if processors:
            problem, reason = _health_problem(processors.get("Status"))
            snapshot.add_binary(
                BinaryReading(
                    key="processor_health",
                    name="Processor health",
                    value=problem,
                    reason=reason,
                    device_class=BinarySensorDeviceClass.PROBLEM,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={
                        "count": to_int(processors.get("Count")),
                        "model": processors.get("Model"),
                    },
                )
            )

        if system.get("BiosVersion"):
            snapshot.add(
                Reading(
                    key="bios_version",
                    name="BIOS version",
                    value=system["BiosVersion"],
                    icon="mdi:chip",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={"firmware": self._slow.get("firmware")},
                )
            )
            # Supermicro publishes no machine-readable "latest BIOS" feed, so
            # this reports the installed build rather than offering an install.
            snapshot.add_update(
                UpdateReading(
                    key="bios",
                    name="BIOS",
                    installed_version=system["BiosVersion"],
                    latest_version=system["BiosVersion"],
                    title="System BIOS",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    enabled_default=False,
                )
            )

    def _add_power_control(
        self, snapshot: Snapshot, system: dict[str, Any], manager: dict[str, Any]
    ) -> None:
        """Expose chassis power as a switch plus per-action buttons."""
        reset = (system.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
        target = reset.get("target")
        if not target:
            return
        allowed = {
            str(v)
            for v in (
                reset.get("ResetType@Redfish.AllowableValues")
                or reset.get("ResetType@Redfish.AllowableValues".lower())
                or []
            )
        }
        # Some BMCs omit the allowable-values annotation; assume the basics.
        if not allowed:
            allowed = {"On", "ForceOff", "GracefulShutdown", "ForceRestart"}

        power_state = str(system.get("PowerState") or "")
        off_type = (
            "GracefulShutdown"
            if self._power_off_action == POWER_OFF_GRACEFUL
            and "GracefulShutdown" in allowed
            else "ForceOff"
        )
        snapshot.add_switch(
            SwitchSpec(
                key="power",
                name="Power",
                value=None if not power_state else power_state == "On",
                turn=partial(self._set_power, target, off_type),
                device_class=SwitchDeviceClass.OUTLET,
                icon="mdi:server",
                # The BMC takes several seconds to reflect a state change, and
                # a graceful shutdown depends on the OS responding at all.
                assumed_state=True,
                attributes={"power_state": power_state, "off_action": off_type},
            )
        )

        for reset_type, name, icon, device_class in _RESET_BUTTONS:
            if reset_type not in allowed:
                continue
            snapshot.add_button(
                ButtonSpec(
                    key=f"power_{slugify(reset_type)}",
                    name=name,
                    press=partial(self._reset, target, reset_type),
                    icon=icon,
                    device_class=device_class,
                    # Abrupt actions stay hidden until deliberately enabled.
                    enabled_default=reset_type in ("On", "GracefulShutdown"),
                )
            )

        manager_reset = (manager.get("Actions") or {}).get("#Manager.Reset") or {}
        if manager_reset.get("target"):
            snapshot.add_button(
                ButtonSpec(
                    key="bmc_reset",
                    name="Reboot BMC",
                    press=partial(
                        self._reset, manager_reset["target"], "GracefulRestart"
                    ),
                    device_class=ButtonDeviceClass.RESTART,
                    entity_category=EntityCategory.CONFIG,
                    enabled_default=False,
                )
            )

    async def _reset(self, target: str, reset_type: str) -> None:
        await self._post(target, {"ResetType": reset_type})

    async def _set_power(self, target: str, off_type: str, on: bool) -> None:
        await self._reset(target, "On" if on else off_type)

    async def _add_thermal_and_power(
        self, snapshot: Snapshot, chassis: dict[str, Any]
    ) -> None:
        if not chassis:
            return
        thermal = await self._get(self._odata(chassis, "Thermal"))
        power = await self._get(self._odata(chassis, "Power"))

        if isinstance(thermal, dict):
            self._add_legacy_thermal(snapshot, thermal)
        if isinstance(power, dict):
            self._add_legacy_power(snapshot, power)
        if not isinstance(thermal, dict) and not isinstance(power, dict):
            # Newer schema: a flat Sensors collection instead of Thermal/Power.
            await self._add_sensors_collection(snapshot, chassis)

    def _add_legacy_thermal(self, snapshot: Snapshot, thermal: dict[str, Any]) -> None:
        for index, entry in enumerate(thermal.get("Temperatures") or []):
            if not isinstance(entry, dict):
                continue
            value = to_float(entry.get("ReadingCelsius"))
            name = entry.get("Name") or f"Temperature {index}"
            if value is None:
                continue
            snapshot.add(
                Reading(
                    key=f"temp_{slugify(entry.get('MemberId') or name)}",
                    name=str(name),
                    value=value,
                    device_class=SensorDeviceClass.TEMPERATURE,
                    unit=UnitOfTemperature.CELSIUS,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=0,
                    attributes={
                        "upper_critical": to_float(
                            entry.get("UpperThresholdCritical")
                        ),
                        "upper_non_critical": to_float(
                            entry.get("UpperThresholdNonCritical")
                        ),
                        "physical_context": entry.get("PhysicalContext"),
                        "health": (entry.get("Status") or {}).get("Health"),
                    },
                )
            )

        for index, entry in enumerate(thermal.get("Fans") or []):
            if not isinstance(entry, dict):
                continue
            value = to_float(entry.get("Reading"))
            name = entry.get("Name") or entry.get("FanName") or f"Fan {index}"
            if value is None:
                continue
            units = str(entry.get("ReadingUnits") or "RPM")
            snapshot.add(
                Reading(
                    key=f"fan_{slugify(entry.get('MemberId') or name)}",
                    name=str(name),
                    value=value,
                    unit=PERCENTAGE if units.lower() == "percent" else REVOLUTIONS_PER_MINUTE,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=0,
                    icon="mdi:fan",
                    attributes={
                        "lower_critical": to_float(
                            entry.get("LowerThresholdCritical")
                        ),
                        "health": (entry.get("Status") or {}).get("Health"),
                    },
                )
            )

    def _add_legacy_power(self, snapshot: Snapshot, power: dict[str, Any]) -> None:
        for index, entry in enumerate(power.get("PowerControl") or []):
            if not isinstance(entry, dict):
                continue
            watts = to_float(entry.get("PowerConsumedWatts"))
            if watts is None:
                continue
            suffix = "" if index == 0 else f"_{index}"
            metrics = entry.get("PowerMetrics") or {}
            snapshot.add(
                Reading(
                    key=f"power_consumed{suffix}",
                    name=entry.get("Name") or "Power consumption",
                    value=watts,
                    device_class=SensorDeviceClass.POWER,
                    unit=UnitOfPower.WATT,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=0,
                    attributes={
                        "capacity_watts": to_float(entry.get("PowerCapacityWatts")),
                        "average_watts": to_float(metrics.get("AverageConsumedWatts")),
                        "max_watts": to_float(metrics.get("MaxConsumedWatts")),
                    },
                )
            )

        for index, entry in enumerate(power.get("Voltages") or []):
            if not isinstance(entry, dict):
                continue
            volts = to_float(entry.get("ReadingVolts"))
            name = entry.get("Name") or f"Voltage {index}"
            if volts is None:
                continue
            snapshot.add(
                Reading(
                    key=f"voltage_{slugify(entry.get('MemberId') or name)}",
                    name=str(name),
                    value=volts,
                    device_class=SensorDeviceClass.VOLTAGE,
                    unit=UnitOfElectricPotential.VOLT,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=2,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    enabled_default=False,
                )
            )

        for index, entry in enumerate(power.get("PowerSupplies") or []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("Name") or f"PSU {index + 1}"
            slug = slugify(entry.get("MemberId") or name)
            problem, reason = _health_problem(entry.get("Status"))
            snapshot.add_binary(
                BinaryReading(
                    key=f"psu_{slug}_health",
                    name=f"{name} health",
                    value=problem,
                    reason=reason,
                    device_class=BinarySensorDeviceClass.PROBLEM,
                    attributes={
                        "model": entry.get("Model"),
                        "serial": entry.get("SerialNumber"),
                        "part_number": entry.get("PartNumber"),
                        "firmware": entry.get("FirmwareVersion"),
                        "type": entry.get("PowerSupplyType"),
                        "capacity_watts": to_float(
                            entry.get("PowerCapacityWatts")
                        ),
                        "health": (entry.get("Status") or {}).get("Health"),
                        "state": (entry.get("Status") or {}).get("State"),
                    },
                )
            )
            output = to_float(entry.get("LastPowerOutputWatts"))
            if output is not None:
                snapshot.add(
                    Reading(
                        key=f"psu_{slug}_output",
                        name=f"{name} output",
                        value=output,
                        device_class=SensorDeviceClass.POWER,
                        unit=UnitOfPower.WATT,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=0,
                        entity_category=EntityCategory.DIAGNOSTIC,
                    )
                )
            line_in = to_float(entry.get("LineInputVoltage"))
            if line_in is not None:
                snapshot.add(
                    Reading(
                        key=f"psu_{slug}_input_voltage",
                        name=f"{name} input voltage",
                        value=line_in,
                        device_class=SensorDeviceClass.VOLTAGE,
                        unit=UnitOfElectricPotential.VOLT,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=0,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )

    async def _add_sensors_collection(
        self, snapshot: Snapshot, chassis: dict[str, Any]
    ) -> None:
        """Handle the newer `Chassis/…/Sensors` schema."""
        collection = await self._get(self._odata(chassis, "Sensors"))
        for member in (collection or {}).get("Members") or []:
            path = member.get("@odata.id") if isinstance(member, dict) else None
            sensor = await self._get(path)
            if not isinstance(sensor, dict):
                continue
            value = to_float(sensor.get("Reading"))
            name = sensor.get("Name") or sensor.get("Id")
            if value is None or not name:
                continue
            reading_type = str(
                sensor.get("ReadingType") or sensor.get("ReadingUnits") or ""
            ).lower()
            device_class, unit = _READING_TYPES.get(reading_type, (None, None))
            snapshot.add(
                Reading(
                    key=f"sensor_{slugify(sensor.get('Id') or name)}",
                    name=str(name),
                    value=value,
                    device_class=device_class,
                    unit=unit,
                    state_class=SensorStateClass.MEASUREMENT,
                    icon=None if device_class else "mdi:gauge",
                    attributes={
                        "reading_type": sensor.get("ReadingType"),
                        "health": (sensor.get("Status") or {}).get("Health"),
                    },
                )
            )

    def _add_manager(self, snapshot: Snapshot, manager: dict[str, Any]) -> None:
        if not manager:
            return
        firmware = manager.get("FirmwareVersion")
        if firmware:
            snapshot.add(
                Reading(
                    key="bmc_firmware",
                    name="BMC firmware",
                    value=firmware,
                    icon="mdi:chip",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={"firmware_inventory": self._slow.get("firmware")},
                )
            )
        problem, reason = _health_problem(manager.get("Status"))
        snapshot.add_binary(
            BinaryReading(
                key="bmc_health",
                name="BMC health",
                value=problem,
                reason=reason,
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        )

    def _add_indicator(self, snapshot: Snapshot, chassis: dict[str, Any]) -> None:
        """Chassis identify LED, for locating a machine in the rack."""
        chassis_path = chassis.get("@odata.id")
        if not chassis_path:
            return
        if "LocationIndicatorActive" in chassis:
            active = bool(chassis.get("LocationIndicatorActive"))
            turn = partial(self._set_location_indicator, chassis_path)
        elif "IndicatorLED" in chassis:
            active = str(chassis.get("IndicatorLED") or "").lower() in (
                "lit",
                "blinking",
            )
            turn = partial(self._set_indicator_led, chassis_path)
        else:
            return
        snapshot.add_switch(
            SwitchSpec(
                key="identify_led",
                name="Identify LED",
                value=active,
                turn=turn,
                icon="mdi:led-on",
                entity_category=EntityCategory.CONFIG,
            )
        )

    async def _set_location_indicator(self, path: str, on: bool) -> None:
        await self._patch(path, {"LocationIndicatorActive": on})

    async def _set_indicator_led(self, path: str, on: bool) -> None:
        await self._patch(path, {"IndicatorLED": "Lit" if on else "Off"})

    # -- slow tier --------------------------------------------------------

    async def _collect_firmware(self, root: dict[str, Any]) -> dict[str, str]:
        """Read the firmware inventory.

        Returns the flat name->version map used as a sensor attribute, and
        stashes the full records so each component can also become its own
        update entity.
        """
        update_service = await self._get(self._odata(root, "UpdateService"))
        inventory_path = self._odata(update_service or {}, "FirmwareInventory")
        collection = await self._get(inventory_path)
        result: dict[str, str] = {}
        items: list[dict[str, Any]] = []
        for member in (collection or {}).get("Members") or []:
            path = member.get("@odata.id") if isinstance(member, dict) else None
            resource = await self._get(path)
            if not isinstance(resource, dict):
                continue
            name = resource.get("Name") or resource.get("Id")
            version = resource.get("Version")
            if name and version:
                result[str(name)] = str(version)
                items.append(
                    {
                        "id": resource.get("Id") or name,
                        "name": str(name),
                        "version": str(version),
                        "updateable": truthy(resource.get("Updateable")),
                        "manufacturer": resource.get("Manufacturer"),
                        "release_date": resource.get("ReleaseDate"),
                        "software_id": resource.get("SoftwareId"),
                    }
                )
        self._slow["firmware_items"] = items
        return result

    def _add_firmware_inventory(self, snapshot: Snapshot) -> None:
        """One update entity per inventoried component.

        Reported, never installed: there is no public feed saying what the
        latest version for a given board is, and Redfish `SimpleUpdate` needs
        an image URI that only the operator can supply. Installed equals
        latest, so these read as up to date and exist to be *seen* — which is
        the part a BMC's own UI makes hard.

        Off by default: a server can inventory dozens of components, and
        creating them all as enabled entities would swamp the device page.
        """
        for item in self._slow.get("firmware_items") or []:
            slug = slugify(str(item.get("id") or item["name"]))
            snapshot.add_update(
                UpdateReading(
                    key=f"fw_{slug}",
                    name=f"{item['name']} firmware",
                    installed_version=item["version"],
                    latest_version=item["version"],
                    title=item["name"],
                    entity_category=EntityCategory.DIAGNOSTIC,
                    enabled_default=False,
                    attributes={
                        "updateable": item.get("updateable"),
                        "manufacturer": item.get("manufacturer"),
                        "release_date": item.get("release_date"),
                        "software_id": item.get("software_id"),
                    },
                )
            )

    async def _collect_log(self, system: dict[str, Any]) -> dict[str, Any]:
        """Summarise the system event log."""
        services = await self._get(self._odata(system, "LogServices"))
        for member in (services or {}).get("Members") or []:
            path = member.get("@odata.id") if isinstance(member, dict) else None
            service = await self._get(path)
            if not isinstance(service, dict):
                continue
            entries = await self._get(self._odata(service, "Entries"))
            if not isinstance(entries, dict):
                continue
            members = [m for m in entries.get("Members") or [] if isinstance(m, dict)]
            count = to_int(entries.get("Members@odata.count"))
            latest = next(
                (m for m in reversed(members) if m.get("Message") or m.get("Created")),
                None,
            )
            return {
                "count": count if count is not None else len(members),
                "latest_message": (latest or {}).get("Message"),
                "latest_severity": (latest or {}).get("Severity"),
                "latest_created": (latest or {}).get("Created"),
                "service": service.get("Name"),
            }
        return {}

    def _add_log(self, snapshot: Snapshot) -> None:
        log = self._slow.get("log") or {}
        if not log:
            return
        snapshot.add(
            Reading(
                key="sel_entries",
                name="Event log entries",
                value=log.get("count"),
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:text-box-search",
                entity_category=EntityCategory.DIAGNOSTIC,
                attributes={
                    "latest_message": log.get("latest_message"),
                    "latest_severity": log.get("latest_severity"),
                    "latest_created": log.get("latest_created"),
                    "log_service": log.get("service"),
                },
            )
        )
        severity = str(log.get("latest_severity") or "").lower()
        raised = severity in ("critical", "warning") if severity else None
        snapshot.add_binary(
            BinaryReading(
                key="sel_critical",
                name="Event log critical entry",
                value=raised,
                # The log entry itself is the explanation.
                reason=log.get("latest_message") if raised else None,
                device_class=BinarySensorDeviceClass.PROBLEM,
                entity_category=EntityCategory.DIAGNOSTIC,
                attributes={"latest_message": log.get("latest_message")},
            )
        )
