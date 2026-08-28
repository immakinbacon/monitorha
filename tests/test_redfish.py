"""Redfish backend tests, shaped like Supermicro X12 BMC output."""

from __future__ import annotations

import pytest

from monitorha.app.api.redfish import RedfishClient
from monitorha.app.const import POWER_OFF_FORCE

RESET_TARGET = "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"

ROUTES = {
    ("GET", "/redfish/v1/"): {
        "RedfishVersion": "1.11.0",
        "UUID": "00000000-0000-0000-0000-3cecef000000",
        "Systems": {"@odata.id": "/redfish/v1/Systems"},
        "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
        "Managers": {"@odata.id": "/redfish/v1/Managers"},
        "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
    },
    ("GET", "/redfish/v1/Systems"): {
        "Members": [{"@odata.id": "/redfish/v1/Systems/1"}]
    },
    ("GET", "/redfish/v1/Systems/1"): {
        "@odata.id": "/redfish/v1/Systems/1",
        "Id": "1",
        "HostName": "vm-host-01",
        "Manufacturer": "Supermicro",
        "Model": "SYS-121H-TNR",
        "SerialNumber": "S123456X7890123",
        "BiosVersion": "1.4",
        "PowerState": "On",
        "Status": {"State": "Enabled", "Health": "OK"},
        "MemorySummary": {
            "TotalSystemMemoryGiB": 256,
            "Status": {"State": "Enabled", "Health": "OK"},
        },
        "ProcessorSummary": {
            "Count": 2,
            "Model": "Intel Xeon Gold 6338",
            "Status": {"State": "Enabled", "Health": "OK"},
        },
        "LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices"},
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": RESET_TARGET,
                "ResetType@Redfish.AllowableValues": [
                    "On",
                    "ForceOff",
                    "GracefulShutdown",
                    "GracefulRestart",
                    "ForceRestart",
                    "Nmi",
                    "ForceOn",
                ],
            }
        },
    },
    ("GET", "/redfish/v1/Chassis"): {
        "Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]
    },
    ("GET", "/redfish/v1/Chassis/1"): {
        "@odata.id": "/redfish/v1/Chassis/1",
        "Manufacturer": "Supermicro",
        "Model": "CSE-119H",
        "SerialNumber": "C987654321",
        "IndicatorLED": "Off",
        "Thermal": {"@odata.id": "/redfish/v1/Chassis/1/Thermal"},
        "Power": {"@odata.id": "/redfish/v1/Chassis/1/Power"},
    },
    ("GET", "/redfish/v1/Chassis/1/Thermal"): {
        "Temperatures": [
            {
                "MemberId": "0",
                "Name": "CPU1 Temp",
                "ReadingCelsius": 47,
                "UpperThresholdCritical": 92,
                "Status": {"State": "Enabled", "Health": "OK"},
            },
            {
                "MemberId": "1",
                "Name": "System Temp",
                "ReadingCelsius": 31,
                "Status": {"State": "Enabled", "Health": "OK"},
            },
            {
                "MemberId": "2",
                "Name": "Absent Sensor",
                "ReadingCelsius": None,
                "Status": {"State": "Absent"},
            },
        ],
        "Fans": [
            {
                "MemberId": "0",
                "Name": "FAN1",
                "Reading": 6300,
                "ReadingUnits": "RPM",
                "LowerThresholdCritical": 700,
                "Status": {"State": "Enabled", "Health": "OK"},
            },
            {
                "MemberId": "1",
                "Name": "FAN2",
                "Reading": 6400,
                "ReadingUnits": "RPM",
                "Status": {"State": "Enabled", "Health": "OK"},
            },
        ],
    },
    ("GET", "/redfish/v1/Chassis/1/Power"): {
        "PowerControl": [
            {
                "Name": "System Power Control",
                "PowerConsumedWatts": 284,
                "PowerCapacityWatts": 1200,
                "PowerMetrics": {"AverageConsumedWatts": 271, "MaxConsumedWatts": 512},
            }
        ],
        "Voltages": [
            {"MemberId": "0", "Name": "12V", "ReadingVolts": 12.08},
            {"MemberId": "1", "Name": "3.3VCC", "ReadingVolts": 3.31},
        ],
        "PowerSupplies": [
            {
                "MemberId": "0",
                "Name": "PSU1",
                "Model": "PWS-1K23A-1R",
                "SerialNumber": "P1K23ACG00",
                "FirmwareVersion": "1.2",
                "PowerSupplyType": "AC",
                "LineInputVoltage": 230,
                "LastPowerOutputWatts": 150,
                "PowerCapacityWatts": 1200,
                "Status": {"State": "Enabled", "Health": "OK"},
            },
            {
                "MemberId": "1",
                "Name": "PSU2",
                "Model": "PWS-1K23A-1R",
                "LastPowerOutputWatts": 134,
                "Status": {"State": "Enabled", "Health": "Critical"},
            },
        ],
    },
    ("GET", "/redfish/v1/Managers"): {
        "Members": [{"@odata.id": "/redfish/v1/Managers/1"}]
    },
    ("GET", "/redfish/v1/Managers/1"): {
        "@odata.id": "/redfish/v1/Managers/1",
        "FirmwareVersion": "01.02.09",
        "Status": {"State": "Enabled", "Health": "OK"},
        "Actions": {
            "#Manager.Reset": {
                "target": "/redfish/v1/Managers/1/Actions/Manager.Reset",
                "ResetType@Redfish.AllowableValues": ["GracefulRestart"],
            }
        },
    },
    ("GET", "/redfish/v1/UpdateService"): {
        "FirmwareInventory": {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"}
    },
    ("GET", "/redfish/v1/UpdateService/FirmwareInventory"): {
        "Members": [
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BMC"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOS"},
            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/NIC.1"},
        ]
    },
    ("GET", "/redfish/v1/UpdateService/FirmwareInventory/BMC"): {
        "Id": "BMC",
        "Name": "BMC",
        "Version": "01.02.09",
        "Updateable": True,
        "Manufacturer": "Supermicro",
        "ReleaseDate": "2025-11-04T00:00:00Z",
    },
    ("GET", "/redfish/v1/UpdateService/FirmwareInventory/NIC.1"): {
        "Id": "NIC.1",
        "Name": "Intel X710",
        "Version": "9.30",
        "Updateable": False,
    },
    ("GET", "/redfish/v1/UpdateService/FirmwareInventory/BIOS"): {
        "Name": "BIOS",
        "Version": "1.4",
    },
    ("GET", "/redfish/v1/Systems/1/LogServices"): {
        "Members": [{"@odata.id": "/redfish/v1/Systems/1/LogServices/Log"}]
    },
    ("GET", "/redfish/v1/Systems/1/LogServices/Log"): {
        "Name": "System Event Log",
        "Entries": {"@odata.id": "/redfish/v1/Systems/1/LogServices/Log/Entries"},
    },
    ("GET", "/redfish/v1/Systems/1/LogServices/Log/Entries"): {
        "Members@odata.count": 3,
        "Members": [
            {"Created": "2026-01-02T10:00:00Z", "Message": "System boot", "Severity": "OK"},
            {
                "Created": "2026-02-11T04:12:00Z",
                "Message": "Power Supply PSU2 failure detected",
                "Severity": "Critical",
            },
        ],
    },
}


@pytest.fixture
def client(make_session):
    return RedfishClient(make_session(ROUTES), "10.0.0.20", 443, "ADMIN", "secret")


@pytest.mark.asyncio
async def test_device_metadata(client):
    snapshot = await client.async_fetch(slow=True)
    device = snapshot.devices["main"]
    assert device.name == "vm-host-01"
    assert device.manufacturer == "Supermicro"
    assert device.model == "SYS-121H-TNR"
    assert device.serial_number == "S123456X7890123"
    assert device.sw_version == "1.4"
    assert device.hw_version == "01.02.09"


@pytest.mark.asyncio
async def test_thermal_sensors(client):
    snapshot = await client.async_fetch(slow=True)
    cpu = snapshot.sensors["temp_0"]
    assert cpu.name == "CPU1 Temp"
    assert cpu.value == 47.0
    assert cpu.unit == "°C"
    assert cpu.attributes["upper_critical"] == 92.0
    assert snapshot.sensors["fan_0"].value == 6300.0
    assert snapshot.sensors["fan_0"].unit == "rpm"
    # A sensor reporting no reading produces no entity.
    assert "temp_2" not in snapshot.sensors


@pytest.mark.asyncio
async def test_power_sensors_and_psu_health(client):
    snapshot = await client.async_fetch(slow=True)
    power = snapshot.sensors["power_consumed"]
    assert power.value == 284.0
    assert power.unit == "W"
    assert power.attributes["max_watts"] == 512.0

    assert snapshot.sensors["voltage_0"].value == 12.08
    assert snapshot.binary_sensors["psu_0_health"].value is False
    assert snapshot.binary_sensors["psu_1_health"].value is True
    assert snapshot.sensors["psu_0_output"].value == 150.0
    assert snapshot.sensors["psu_0_input_voltage"].value == 230.0


@pytest.mark.asyncio
async def test_health_rollups(client):
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.binary_sensors["system_health"].value is False
    assert snapshot.binary_sensors["memory_health"].value is False
    assert snapshot.binary_sensors["processor_health"].value is False
    assert snapshot.binary_sensors["bmc_health"].value is False
    assert snapshot.sensors["power_state"].value == "On"


@pytest.mark.asyncio
async def test_power_switch_graceful_by_default(client):
    snapshot = await client.async_fetch(slow=True)
    power = snapshot.switches["power"]
    assert power.value is True
    assert power.attributes["off_action"] == "GracefulShutdown"

    await power.turn(False)
    method, path, body = client._session.calls[-1]
    assert (method, path) == ("POST", RESET_TARGET)
    assert body == {"ResetType": "GracefulShutdown"}

    await power.turn(True)
    assert client._session.calls[-1][2] == {"ResetType": "On"}


@pytest.mark.asyncio
async def test_power_switch_force_option(make_session):
    client = RedfishClient(
        make_session(ROUTES),
        "10.0.0.20",
        443,
        "ADMIN",
        "secret",
        power_off_action=POWER_OFF_FORCE,
    )
    snapshot = await client.async_fetch(slow=True)
    await snapshot.switches["power"].turn(False)
    assert client._session.calls[-1][2] == {"ResetType": "ForceOff"}


@pytest.mark.asyncio
async def test_reset_buttons_follow_allowable_values(client):
    snapshot = await client.async_fetch(slow=True)
    assert "power_on" in snapshot.buttons
    assert "power_forceoff" in snapshot.buttons
    assert "power_gracefulshutdown" in snapshot.buttons
    assert "power_nmi" in snapshot.buttons
    # PowerCycle is not offered by this BMC, so no button for it.
    assert "power_powercycle" not in snapshot.buttons
    # Abrupt actions are hidden until the user enables them.
    assert snapshot.buttons["power_on"].enabled_default is True
    assert snapshot.buttons["power_forceoff"].enabled_default is False

    await snapshot.buttons["power_forcerestart"].press()
    assert client._session.calls[-1][2] == {"ResetType": "ForceRestart"}

    await snapshot.buttons["bmc_reset"].press()
    assert client._session.calls[-1][1] == "/redfish/v1/Managers/1/Actions/Manager.Reset"


@pytest.mark.asyncio
async def test_identify_led(client):
    snapshot = await client.async_fetch(slow=True)
    led = snapshot.switches["identify_led"]
    assert led.value is False
    await led.turn(True)
    method, path, body = client._session.calls[-1]
    assert (method, path) == ("PATCH", "/redfish/v1/Chassis/1")
    assert body == {"IndicatorLED": "Lit"}


@pytest.mark.asyncio
async def test_firmware_and_event_log(client):
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.sensors["bmc_firmware"].value == "01.02.09"
    inventory = snapshot.sensors["bmc_firmware"].attributes["firmware_inventory"]
    assert inventory == {"BMC": "01.02.09", "BIOS": "1.4", "Intel X710": "9.30"}

    sel = snapshot.sensors["sel_entries"]
    assert sel.value == 3
    assert "PSU2 failure" in sel.attributes["latest_message"]
    assert snapshot.binary_sensors["sel_critical"].value is True


@pytest.mark.asyncio
async def test_newer_sensors_collection_fallback(make_session):
    """Boards without legacy Thermal/Power fall back to the Sensors collection."""
    routes = {
        k: v
        for k, v in ROUTES.items()
        if k[1] not in ("/redfish/v1/Chassis/1/Thermal", "/redfish/v1/Chassis/1/Power")
    }
    routes[("GET", "/redfish/v1/Chassis/1")] = {
        "@odata.id": "/redfish/v1/Chassis/1",
        "Manufacturer": "Supermicro",
        "Model": "CSE-119H",
        "Sensors": {"@odata.id": "/redfish/v1/Chassis/1/Sensors"},
    }
    routes[("GET", "/redfish/v1/Chassis/1/Sensors")] = {
        "Members": [
            {"@odata.id": "/redfish/v1/Chassis/1/Sensors/CPU1Temp"},
            {"@odata.id": "/redfish/v1/Chassis/1/Sensors/TotalPower"},
        ]
    }
    routes[("GET", "/redfish/v1/Chassis/1/Sensors/CPU1Temp")] = {
        "Id": "CPU1Temp",
        "Name": "CPU1 Temp",
        "Reading": 49,
        "ReadingType": "Temperature",
        "Status": {"Health": "OK"},
    }
    routes[("GET", "/redfish/v1/Chassis/1/Sensors/TotalPower")] = {
        "Id": "TotalPower",
        "Name": "Total Power",
        "Reading": 301,
        "ReadingType": "Power",
        "Status": {"Health": "OK"},
    }

    client = RedfishClient(make_session(routes), "10.0.0.20", 443, "ADMIN", "secret")
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.sensors["sensor_cpu1temp"].value == 49.0
    assert snapshot.sensors["sensor_cpu1temp"].unit == "°C"
    assert snapshot.sensors["sensor_totalpower"].value == 301.0
    assert snapshot.sensors["sensor_totalpower"].unit == "W"


@pytest.mark.asyncio
async def test_slow_data_cached_across_fast_polls(client):
    await client.async_fetch(slow=True)
    client._session.calls.clear()
    snapshot = await client.async_fetch(slow=False)
    assert not [c for c in client._session.calls if "FirmwareInventory" in c[1]]
    assert not [c for c in client._session.calls if "LogServices" in c[1]]
    assert snapshot.sensors["sel_entries"].value == 3


@pytest.mark.asyncio
async def test_validate(client):
    info = await client.async_validate()
    assert info["unique_id"] == "S123456X7890123"
    assert info["title"] == "SYS-121H-TNR"


# -- session authentication ----------------------------------------------

SESSIONS_PATH = "/redfish/v1/SessionService/Sessions"
TOKEN = "abc123token"


def basic_refusing_routes():
    """A BMC that serves Redfish but rejects HTTP Basic, as older SMC firmware does.

    Everything past the service root answers 401 unless an X-Auth-Token is
    presented, and the token is only obtainable by POSTing to SessionService.
    """
    routes = {}
    for key, value in ROUTES.items():
        if key[1] == "/redfish/v1/":
            routes[key] = value
            continue

        def guard(headers, _value=value):
            if headers.get("X-Auth-Token") == TOKEN:
                return _value
            from tests.conftest import Status

            return Status(401, {"error": "Basic auth not supported"})

        routes[key] = guard

    from tests.conftest import Status

    routes[("POST", SESSIONS_PATH)] = Status(
        201,
        {"Id": "1"},
        {"X-Auth-Token": TOKEN, "Location": f"{SESSIONS_PATH}/1"},
    )
    return routes


@pytest.mark.asyncio
async def test_falls_back_to_session_auth(make_session):
    session = make_session(basic_refusing_routes())
    client = RedfishClient(session, "10.0.0.20", 443, "ADMIN", "secret")

    snapshot = await client.async_fetch(slow=True)

    # The poll succeeded despite Basic being refused throughout.
    assert snapshot.devices["main"].name == "vm-host-01"
    assert snapshot.sensors["temp_0"].value == 47.0
    assert client._use_session_auth is True

    # Exactly one session was opened, not one per request.
    logins = [c for c in session.calls if c[0] == "POST" and c[1] == SESSIONS_PATH]
    assert len(logins) == 1
    assert logins[0][2] == {"UserName": "ADMIN", "Password": "secret"}


@pytest.mark.asyncio
async def test_session_is_released_on_close(make_session):
    """A leaked session occupies one of the BMC's few slots until it expires."""
    session = make_session(basic_refusing_routes())
    client = RedfishClient(session, "10.0.0.20", 443, "ADMIN", "secret")
    await client.async_fetch(slow=True)

    await client.async_close()
    assert ("DELETE", f"{SESSIONS_PATH}/1", None) in session.calls
    # Closing twice must not fire a second delete.
    session.calls.clear()
    await client.async_close()
    assert not [c for c in session.calls if c[0] == "DELETE"]


@pytest.mark.asyncio
async def test_expired_token_is_renewed(make_session):
    session = make_session(basic_refusing_routes())
    client = RedfishClient(session, "10.0.0.20", 443, "ADMIN", "secret")
    await client.async_fetch(slow=True)

    # The BMC forgets the session; the next poll must re-open one, not fail.
    client._token = "stale"
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.sensors["temp_0"].value == 47.0

    logins = [c for c in session.calls if c[0] == "POST" and c[1] == SESSIONS_PATH]
    assert len(logins) == 2


@pytest.mark.asyncio
async def test_basic_auth_is_preferred_when_it_works(make_session):
    """A BMC that accepts Basic must not have sessions created against it."""
    session = make_session(ROUTES)
    client = RedfishClient(session, "10.0.0.20", 443, "ADMIN", "secret")
    await client.async_fetch(slow=True)

    assert client._use_session_auth is False
    assert not [c for c in session.calls if c[1] == SESSIONS_PATH]


@pytest.mark.asyncio
async def test_bad_credentials_still_fail_clearly(make_session):
    """When neither scheme works the error must name both, not just Basic."""
    from monitorha.app.api.base import AuthenticationError
    from tests.conftest import Status

    routes = {("GET", "/redfish/v1/"): ROUTES[("GET", "/redfish/v1/")]}
    routes[("GET", "/redfish/v1/Systems")] = Status(401)
    routes[("POST", SESSIONS_PATH)] = Status(401, {"error": "bad credentials"})

    client = RedfishClient(make_session(routes), "10.0.0.20", 443, "ADMIN", "wrong")
    with pytest.raises(AuthenticationError) as err:
        await client.async_fetch(slow=True)
    assert "Basic" in str(err.value) and "session" in str(err.value)


@pytest.mark.asyncio
async def test_sessions_path_comes_from_the_service_root(make_session):
    """Firmware that advertises a non-standard session path must be honoured."""
    routes = dict(basic_refusing_routes())
    root = dict(ROUTES[("GET", "/redfish/v1/")])
    root["Links"] = {"Sessions": {"@odata.id": "/redfish/v1/Oem/Sessions"}}
    routes[("GET", "/redfish/v1/")] = root
    from tests.conftest import Status

    routes[("POST", "/redfish/v1/Oem/Sessions")] = Status(
        201, {"Id": "1"}, {"X-Auth-Token": TOKEN, "Location": "/redfish/v1/Oem/Sessions/1"}
    )
    del routes[("POST", SESSIONS_PATH)]

    session = make_session(routes)
    client = RedfishClient(session, "10.0.0.20", 443, "ADMIN", "secret")
    await client.async_fetch(slow=True)
    assert [c for c in session.calls if c[1] == "/redfish/v1/Oem/Sessions"]


@pytest.mark.asyncio
async def test_post_survives_a_trailing_slash_redirect(make_session):
    """Firmware that 301s collection URIs must still receive POST bodies.

    aiohttp's own redirect handling rewrites POST to GET on a 301 and drops the
    body, so a session login would arrive with no credentials at all.
    """
    from tests.conftest import Status

    routes = dict(basic_refusing_routes())
    del routes[("POST", SESSIONS_PATH)]
    routes[("POST", SESSIONS_PATH)] = Status(
        301, None, {"Location": f"https://10.0.0.20:443{SESSIONS_PATH}/"}
    )
    routes[("POST", f"{SESSIONS_PATH}/")] = Status(
        201, {"Id": "1"}, {"X-Auth-Token": TOKEN, "Location": f"{SESSIONS_PATH}/1"}
    )

    session = make_session(routes)
    client = RedfishClient(session, "10.0.0.20", 443, "ADMIN", "secret")
    snapshot = await client.async_fetch(slow=True)

    assert snapshot.sensors["temp_0"].value == 47.0
    # The credentials reached the redirect target, still as a POST.
    final = [c for c in session.calls if c[1] == f"{SESSIONS_PATH}/"]
    assert final and final[0][0] == "POST"
    assert final[0][2] == {"UserName": "ADMIN", "Password": "secret"}


@pytest.mark.asyncio
async def test_power_action_survives_a_redirect(make_session):
    """A 301 must not silently downgrade a reset action into a no-op GET."""
    from tests.conftest import Status

    routes = dict(ROUTES)
    routes[("POST", RESET_TARGET)] = Status(
        301, None, {"Location": f"https://10.0.0.20:443{RESET_TARGET}/"}
    )
    routes[("POST", f"{RESET_TARGET}/")] = Status(204, None)

    session = make_session(routes)
    client = RedfishClient(session, "10.0.0.20", 443, "ADMIN", "secret")
    snapshot = await client.async_fetch(slow=True)
    await snapshot.switches["power"].turn(False)

    landed = [c for c in session.calls if c[1] == f"{RESET_TARGET}/"]
    assert landed and landed[0][0] == "POST"
    assert landed[0][2] == {"ResetType": "GracefulShutdown"}


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_refused(make_session):
    """Credentials must never be replayed to a different host."""
    from monitorha.app.api.base import ConnectionFailed
    from tests.conftest import Status

    routes = dict(ROUTES)
    routes[("GET", "/redfish/v1/")] = Status(
        301, None, {"Location": "https://evil.example/redfish/v1/"}
    )
    client = RedfishClient(make_session(routes), "10.0.0.20", 443, "ADMIN", "secret")
    with pytest.raises(ConnectionFailed) as err:
        await client.async_fetch(slow=True)
    assert "different origin" in str(err.value)


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        # The case that blocked a real BMC: explicit default port vs implicit.
        ("https://bmc.example:443/a", "https://bmc.example/a/", True),
        ("http://bmc.example:80/a", "http://bmc.example/a/", True),
        ("https://bmc.example/a", "https://bmc.example:443/a/", True),
        ("https://bmc.example:8443/a", "https://bmc.example/a/", False),
        ("https://bmc.example:443/a", "http://bmc.example:443/a", False),
        ("https://bmc.example:443/a", "https://evil.example/a", False),
        ("https://BMC.example:443/a", "https://bmc.example/a", True),
    ],
)
def test_origin_comparison_normalises_default_ports(first, second, same):
    from monitorha.app.api.base import _same_origin

    assert _same_origin(first, second) is same


@pytest.mark.asyncio
async def test_redirect_dropping_the_default_port_is_followed(make_session):
    """Clients emit an explicit :443; devices omit it when redirecting."""
    from tests.conftest import Status

    routes = dict(ROUTES)
    routes[("GET", "/redfish/v1/Systems")] = Status(
        301, None, {"Location": "https://10.0.0.20/redfish/v1/Systems/"}
    )
    routes[("GET", "/redfish/v1/Systems/")] = ROUTES[("GET", "/redfish/v1/Systems")]

    session = make_session(routes)
    client = RedfishClient(session, "10.0.0.20", 443, "ADMIN", "secret")
    snapshot = await client.async_fetch(slow=True)

    assert snapshot.devices["main"].name == "vm-host-01"
    assert [c for c in session.calls if c[1] == "/redfish/v1/Systems/"]


# -- saying what the problem is ------------------------------------------


def test_health_problem_reports_the_firmware_message() -> None:
    """A BMC's own wording beats a generic rollup."""
    from monitorha.app.api.redfish import _health_problem

    problem, reason = _health_problem(
        {
            "State": "Enabled",
            "Health": "Critical",
            "Conditions": [{"Message": "Power Supply 2 has failed"}],
        }
    )
    assert problem is True
    assert reason == "Power Supply 2 has failed"


def test_health_problem_falls_back_to_the_rollup() -> None:
    from monitorha.app.api.redfish import _health_problem

    _, reason = _health_problem({"State": "Enabled", "Health": "Warning"})
    assert reason == "Health is Warning"


def test_health_problem_mentions_an_unusual_state() -> None:
    from monitorha.app.api.redfish import _health_problem

    _, reason = _health_problem({"State": "UnavailableOffline", "Health": "Critical"})
    assert reason == "Health is Critical (state UnavailableOffline)"


def test_health_problem_uses_message_id_when_there_is_no_message() -> None:
    from monitorha.app.api.redfish import _health_problem

    _, reason = _health_problem(
        {
            "State": "Enabled",
            "Health": "Critical",
            "Conditions": [{"MessageId": "Alert.1.0.PowerSupplyFailure"}],
        }
    )
    assert reason == "Alert.1.0.PowerSupplyFailure"


def test_a_healthy_status_needs_no_explanation() -> None:
    from monitorha.app.api.redfish import _health_problem

    assert _health_problem({"State": "Enabled", "Health": "OK"}) == (False, None)


def test_an_absent_part_is_neither_healthy_nor_a_problem() -> None:
    from monitorha.app.api.redfish import _health_problem

    assert _health_problem({"State": "Absent", "Health": "Critical"}) == (None, None)


async def test_system_health_carries_its_reason(make_session) -> None:
    """End to end: the explanation reaches the snapshot, not just the helper."""
    import copy

    routes = copy.deepcopy(ROUTES)
    system = copy.deepcopy(routes[("GET", "/redfish/v1/Systems/1")])
    system["Status"] = {
        "State": "Enabled",
        "Health": "Critical",
        "Conditions": [{"Message": "CPU 1 over temperature"}],
    }
    routes[("GET", "/redfish/v1/Systems/1")] = system

    client = RedfishClient(
        make_session(routes), "10.0.0.20", 443, username="ADMIN", password="secret"
    )
    snapshot = await client.async_fetch(slow=True)
    health = snapshot.binary_sensors["system_health"]
    assert health.value is True
    assert health.reason == "CPU 1 over temperature"


# -- firmware inventory --------------------------------------------------


async def test_every_inventoried_component_becomes_an_update_entity(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.updates["fw_bmc"].installed_version == "01.02.09"
    assert snapshot.updates["fw_nic_1"].name == "Intel X710 firmware"


async def test_firmware_entities_report_rather_than_offer_an_install(client) -> None:
    """There is no feed for "latest", and SimpleUpdate needs an image URI."""
    snapshot = await client.async_fetch(slow=True)
    entry = snapshot.updates["fw_bmc"]
    assert entry.install is None
    assert entry.installed_version == entry.latest_version


async def test_firmware_entities_are_off_by_default(client) -> None:
    """A server can inventory dozens of these; enabling them all would swamp it."""
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.updates["fw_bmc"].enabled_default is False


async def test_firmware_entities_record_what_the_bmc_would_accept(client) -> None:
    snapshot = await client.async_fetch(slow=True)
    assert snapshot.updates["fw_bmc"].attributes["updateable"] is True
    assert snapshot.updates["fw_nic_1"].attributes["updateable"] is False
    assert snapshot.updates["fw_bmc"].attributes["manufacturer"] == "Supermicro"


async def test_the_existing_bios_entity_is_unchanged(client) -> None:
    """It is enabled by default; the inventory additions must not displace it."""
    snapshot = await client.async_fetch(slow=True)
    assert "bios" in snapshot.updates
