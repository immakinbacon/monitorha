"""SwOS backend tests.

The SwOS Lite payloads are what a CSS610-8P-2S+ on 2.21 actually answers with,
with its identifiers replaced; the SwOS ones are the same data spelled the way
the CRS/CSS3xx firmware spells it. Keeping both means a change to the shared
decoding cannot quietly work for one dialect and break the other.
"""

from __future__ import annotations

import hashlib

import pytest

from monitorha.app.api.swos import (
    DigestAuth,
    SwosClient,
    find_upgrade_product,
    is_newer,
    parse_swos,
)

from .conftest import Raw, Status

# -- captured payloads ---------------------------------------------------

SYS_LITE = (
    "{i01:0x0011375a,i02:0xea03000a,i03:'48a98a000001',"
    "i04:'4142313243443334454635',i05:'737769746368312e6578616d706c65',"
    "i06:'322e3231',i0b:0x694407aa,i07:'4353533631302d38502d32532b',"
    "i12:0x0100,i08:0x0301,i21:0x00,i09:0xea03000a,i0a:0x01,i0c:0x00,"
    "i0d:0x01,i0e:0x8000,i0f:0x00,i2a:0x00,i10:0x8000,i11:'04f41c000002',"
    "i13:0x03ff,i14:0x01,i15:0x0af0,i1e:0x129f,i16:0x00b9,i1f:0x0224,"
    "i1c:0x00,i1d:0x00,i17:0x00,i29:0x00,i27:0x0000,i28:0x01,"
    "i19:0x0000000a,i1a:0x14,i1b:0x0067,i20:0x00,i22:0x003c,i23:0x00,"
    "i24:0x0000,i25:0x4110,i26:0x0122,i2b:''}"
)

LINK_LITE = (
    "{i01:0x03ff,i0c:0x0000,i02:0x03ff,i03:0x03ff,i16:0x0000,i12:0x0000,"
    "i05:[0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00],"
    "i06:0x03f7,i07:0x03f7,i13:0x0000,i14:0x0000,i15:0x0000,i0b:0x0000,"
    "i08:[0x01,0x02,0x01,0x07,0x01,0x01,0x01,0x01,0x03,0x03],"
    "i09:[0x0000010c,0x0000d96b,0x000069d6,0x00000000,0x000069a4,"
    "0x000069a4,0x000069a4,0x00006972,0x000000a8,0x000000a8],"
    "i0a:['706f727431202d20746e7230','706f727432202d20617031',"
    "'706f727433202d2063616d657261','706f727434202d2063616d657261',"
    "'706f727435202d2063616d657261','706f727436202d2063616d657261',"
    "'706f727437202d2063616d657261','706f727438202d2063616d657261',"
    "'5346502b31202d2073776974636830','5346502b32202d2073776974636832']}"
)

POE_LITE = (
    "{i01:[0x02,0x02,0x02,0x02,0x02,0x02,0x02,0x02,0x02,0x02],"
    "i02:[0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x00,0x01],"
    "i03:[0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00],"
    "i04:[0x03,0x03,0x03,0x02,0x03,0x03,0x03,0x03,0x00,0x00],"
    "i05:[0x0038,0x00b7,0x0077,0x0000,0x004e,0x0066,0x0063,0x006b,"
    "0x0000,0x0000],"
    "i06:[0x01d3,0x0112,0x01d4,0x0000,0x01d6,0x01d0,0x01d5,0x01d1,"
    "0x0000,0x0000],"
    "i07:[0x0017,0x0031,0x0033,0x0000,0x0020,0x002e,0x002a,0x002e,"
    "0x0000,0x0000],"
    "i08:0x8902,i09:'5851483054455354313233',i0a:0x0000,"
    "i0b:[0x0000,0x0000,0x0000,0x0000,0x0000,0x0000,0x0000,0x0000,"
    "0x0000,0x0000]}"
)

# One fibre module and one direct-attach copper cable. The DAC has no
# diagnostics and says so with -128 in the temperature field.
SFP_LITE = (
    "{i01:['4f454d202020202020202020202020','4f454d202020202020202020202020'],"
    "i02:['5346502d3130472d53522020202020','5346502d48313047422d4355314d20'],"
    "i03:['30322020','52202020'],"
    "i04:['43533130354e423137363320202020','435343323430323030313530303532'],"
    "i05:['32332d31312d3037','32342d30322d3137'],"
    "i06:['7b303335327d6e6d206d756c74692d6d6f6465206669626572',"
    "'7b30317d6d20636f70706572'],"
    "i07:[0x00014c08,0x00000000],i08:[0x002b,0xff80],i09:[0x0cba,0x0000],"
    "i0a:[0x0005,0x0000],i0b:[0x15f7,0x0000],i0c:[0x1437,0x0000]}"
)

# Trimmed to the four counter fields the backend reads; the switch returns
# some forty of them, all shaped the same way.
STATS_LITE = (
    "{i01:[0x03d278f0,0xe0a4396b,0x3ff48ebb,0x00000000,0x85b95d50,"
    "0x0130a709,0xec719cfd,0x8ecb2eb3,0x6dfb1d45,0x181f23c9],"
    "i02:[0x00000000,0x00000000,0x00000073,0x00000000,0x000000c0,"
    "0x00000148,0x0000012a,0x000000df,0x00000011,0x00000000],"
    "i0f:[0x1e66ae10,0xa0fbf9f0,0x106abc9b,0x00000000,0x98df2a8d,"
    "0xf7912f5d,0xb6662867,0x277a57e2,0x88fc2c4d,0x7978a398],"
    "i10:[0x00000000,0x00000001,0x00000002,0x00000000,0x00000002,"
    "0x00000003,0x00000003,0x00000003,0x0000048a,0x00000000]}"
)

# The web UI, which is where the product code for the upgrade server lives.
ENGINE_LITE = (
    'function Ab(a){zb("http://upgrade.mikrotik.com/swoslite/css610pi/LATEST",'
    'null,function(g){});}'
)

# SwOS names its fields rather than numbering them, counts uptime in
# hundredths of a second, and reports its port and cage counts outright.
SYS_FULL = (
    "{upt:0x0011375a,cip:0xea03000a,mac:'48a98a000001',"
    "sid:'4142313243443334454635',id:'636f72652d737769746368',"
    "ver:'322e3137',bld:0x66000000,brd:'4352533331302d38472b32532b',"
    "mrkt:'4352533331302d38472b32532b',rev:'72312e30',temp:0x002f,"
    "btm1:0x0025,fan1:0x0708,p1v:0x0af0,p1c:0x00b9,p1s:0x01,p2s:0x00}"
)

LINK_FULL = (
    "{prt:0x000a,sfp:0x0002,en:0x03ff,lnk:0x0003,paus:0x0002,"
    "nm:['657468657231','657468657232'],spd:[0x02,0x03],dpx:0x0003}"
)

STATS_FULL = (
    "{rb:[0x03d278f0,0xe0a4396b],rbh:[0x00000000,0x00000001],"
    "tb:[0x1e66ae10,0xa0fbf9f0],tbh:[0x00000000,0x00000000]}"
)


def digest_routes(bodies: dict[str, str]) -> dict[tuple[str, str], object]:
    """Wrap bodies in the challenge/response SwOS actually demands.

    An unauthenticated request is refused with a nonce; only a request
    carrying a digest response gets the payload, so a client that fell back to
    Basic authentication would fail these tests rather than pass them.
    """

    def route(body: str):
        def handler(headers: dict[str, str]):
            authorization = headers.get("Authorization", "")
            if not authorization.startswith("Digest "):
                return Status(
                    401,
                    None,
                    headers={
                        "WWW-Authenticate": (
                            'Digest realm="CSS610-8P-2S+", qop="auth", '
                            'nonce="a6b3ae01", stale=FALSE'
                        )
                    },
                )
            return Raw(body)

        return handler

    return {("GET", path): route(body) for path, body in bodies.items()}


LITE_ROUTES = digest_routes(
    {
        "/sys.b": SYS_LITE,
        "/link.b": LINK_LITE,
        "/poe.b": POE_LITE,
        "/sfp.b": SFP_LITE,
        "/!stats.b": STATS_LITE,
        "/engine.js": ENGINE_LITE,
    }
)


def make_client(session, **kwargs) -> SwosClient:
    return SwosClient(session, "10.0.3.234", 80, "admin", "secret", **kwargs)


# -- wire format ---------------------------------------------------------


def test_parse_swos_decodes_hex_numbers_strings_and_arrays() -> None:
    parsed = parse_swos("{i01:0x03ff,i0a:['506f727431',''],i05:[0x00,0x0a]}")
    assert parsed == {"i01": 1023, "i0a": ["506f727431", ""], "i05": [0, 10]}


def test_parse_swos_handles_a_list_of_objects() -> None:
    # The host table is a list rather than an object.
    assert parse_swos("[{i01:'00005e000167',i02:0x08}]") == [
        {"i01": "00005e000167", "i02": 8}
    ]


def test_parse_swos_rejects_a_page_of_html() -> None:
    from monitorha.app.api.base import ConnectionFailed

    with pytest.raises(ConnectionFailed):
        parse_swos("<h1>401 Unauthorized</h1>")


# -- authentication ------------------------------------------------------


def test_digest_auth_answers_the_challenge() -> None:
    auth = DigestAuth("admin", "secret")
    assert auth.take_challenge(
        'Digest realm="CSS610-8P-2S+", qop="auth", nonce="a6b3ae01"'
    )
    header = auth.authorization("GET", "/sys.b")

    assert header is not None and header.startswith("Digest ")
    fields = dict(
        part.strip().split("=", 1) for part in header[len("Digest ") :].split(", ")
    )
    unquoted = {k: v.strip('"') for k, v in fields.items()}

    def md5(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    ha1 = md5("admin:CSS610-8P-2S+:secret")
    ha2 = md5("GET:/sys.b")
    expected = md5(
        f"{ha1}:a6b3ae01:{unquoted['nc']}:{unquoted['cnonce']}:auth:{ha2}"
    )
    assert unquoted["response"] == expected
    assert unquoted["uri"] == "/sys.b"


def test_digest_auth_ignores_a_basic_challenge() -> None:
    auth = DigestAuth("admin", "secret")
    assert not auth.take_challenge('Basic realm="switch"')
    assert auth.authorization("GET", "/sys.b") is None


async def test_credentials_are_rejected_cleanly(make_session) -> None:
    from monitorha.app.api.base import AuthenticationError

    def always_401(headers: dict[str, str]):
        return Status(
            401, None, headers={"WWW-Authenticate": 'Digest realm="s", nonce="n"'}
        )

    client = make_client(make_session({("GET", "/sys.b"): always_401}))
    with pytest.raises(AuthenticationError):
        await client.async_fetch(slow=False)


# -- SwOS Lite -----------------------------------------------------------


@pytest.fixture
async def lite(make_session):
    session = make_session(dict(LITE_ROUTES))
    snapshot = await make_client(session, check_firmware=False).async_fetch(slow=False)
    return snapshot, session


async def test_validate_identifies_the_switch(make_session) -> None:
    client = make_client(make_session(dict(LITE_ROUTES)))
    info = await client.async_validate()
    assert info["unique_id"] == "AB12CD34EF5"
    assert info["title"] == "switch1.example"
    assert info["model"] == "CSS610-8P-2S+"
    assert info["firmware"] == "SwOS Lite"


async def test_device_and_system_readings(lite) -> None:
    snapshot, _ = lite
    device = snapshot.devices["main"]
    assert (device.name, device.manufacturer) == ("switch1.example", "MikroTik")
    assert (device.model, device.sw_version) == ("CSS610-8P-2S+", "2.21")
    assert device.serial_number == "AB12CD34EF5"

    assert snapshot.sensors["cpu_temperature"].value == 60
    # 0x0af0 and 0x129f in hundredths of a volt.
    assert snapshot.sensors["psu1_voltage"].value == 28.0
    assert snapshot.sensors["psu2_voltage"].value == 47.67
    # 0x0122 in tenths of a watt, which is the sum of the PoE draw plus the
    # switch itself.
    assert snapshot.sensors["power_consumption"].value == 29.0
    assert snapshot.sensors["version"].attributes["ip_address"] == "10.0.3.234"
    assert snapshot.sensors["version"].attributes["mac_address"] == "48:A9:8A:00:00:01"


async def test_ports_carry_their_configured_names(lite) -> None:
    snapshot, _ = lite
    assert snapshot.binary_sensors["port1_link"].name == "port1 - tnr0 link"
    assert snapshot.sensors["port9_speed"].name == "SFP+1 - switch0 speed"


async def test_link_state_and_speed(lite) -> None:
    snapshot, _ = lite
    # 0x03f7 has bit 3 clear: port 4 is the one with nothing plugged in.
    assert snapshot.binary_sensors["port4_link"].value is False
    assert snapshot.binary_sensors["port1_link"].value is True
    assert snapshot.sensors["port1_speed"].value == "100M"
    assert snapshot.sensors["port2_speed"].value == "1G"
    assert snapshot.sensors["port9_speed"].value == "10G"
    # A speed code past the end of the list is how "no link" is spelled.
    assert snapshot.sensors["port4_speed"].value is None
    assert snapshot.binary_sensors["port2_link"].attributes["full_duplex"] is True


async def test_byte_counters_recombine_both_halves(lite) -> None:
    snapshot, _ = lite
    # Port 3 has run past 2^32 bytes, so the high field carries the rest.
    assert snapshot.sensors["port3_rx_bytes"].value == 0x73 * 2**32 + 0x3FF48EBB
    assert snapshot.sensors["port1_rx_bytes"].value == 0x03D278F0
    # Counters are off by default: ten ports of them would swamp a dashboard.
    assert snapshot.sensors["port3_rx_bytes"].enabled_default is False


async def test_poe_readings(lite) -> None:
    snapshot, _ = lite
    assert snapshot.sensors["port1_poe_status"].value == "powered on"
    assert snapshot.sensors["port1_poe_power"].value == 2.3
    assert snapshot.sensors["port2_poe_voltage"].value == 27.4
    assert snapshot.sensors["port1_poe_current"].value == 0.056
    # An empty PoE port is waiting, not faulted.
    assert snapshot.sensors["port4_poe_status"].value == "waiting for load"
    assert snapshot.binary_sensors["port4_poe_problem"].value is False
    # The SFP cages have no PoE hardware and so get no PoE entities.
    assert "port9_poe_status" not in snapshot.sensors


async def test_poe_fault_explains_itself(make_session) -> None:
    faulted = POE_LITE.replace(
        "i04:[0x03,0x03,0x03,0x02", "i04:[0x04,0x03,0x03,0x02"
    )
    routes = dict(LITE_ROUTES)
    routes.update(digest_routes({"/poe.b": faulted}))
    snapshot = await make_client(
        make_session(routes), check_firmware=False
    ).async_fetch(slow=False)

    problem = snapshot.binary_sensors["port1_poe_problem"]
    assert problem.value is True
    assert problem.reason == "port1 - tnr0 PoE reports overload"


async def test_sfp_diagnostics(lite) -> None:
    snapshot, _ = lite
    temperature = snapshot.sensors["sfp1_temperature"]
    assert temperature.value == 43
    assert temperature.name == "SFP+1 - switch0 temperature"
    assert temperature.attributes["part_number"] == "SFP-10G-SR"
    assert snapshot.sensors["sfp1_voltage"].value == 3.258
    # 0x15f7 tenths of a microwatt, which is what the cage's own display
    # converts to dBm.
    assert snapshot.sensors["sfp1_tx_power"].value == pytest.approx(-2.5, abs=0.01)
    # The direct-attach cable has no diagnostics to report.
    assert "sfp2_temperature" not in snapshot.sensors


async def test_reboot_button_posts_to_the_switch(lite) -> None:
    snapshot, session = lite
    await snapshot.buttons["reboot"].press()
    assert ("POST", "/reboot", "*") in session.calls


async def test_sections_can_be_turned_off(make_session) -> None:
    snapshot = await make_client(
        make_session(dict(LITE_ROUTES)),
        monitor_poe=False,
        monitor_sfp=False,
        check_firmware=False,
    ).async_fetch(slow=False)
    assert not [k for k in snapshot.sensors if "poe" in k or k.startswith("sfp")]
    # Ports are still there: only the sections asked for are dropped.
    assert "port1_link" in snapshot.binary_sensors


async def test_an_endpoint_the_model_lacks_degrades(make_session) -> None:
    """A switch with no PoE redirects `poe.b` rather than answering 404."""
    routes = dict(LITE_ROUTES)
    routes[("GET", "/poe.b")] = Status(
        303, None, headers={"Location": "http://10.0.3.234/index.html"}
    )
    snapshot = await make_client(
        make_session(routes), check_firmware=False
    ).async_fetch(slow=False)
    assert not [k for k in snapshot.sensors if "poe" in k]
    assert snapshot.sensors["cpu_temperature"].value == 60


# -- firmware ------------------------------------------------------------


def test_upgrade_product_from_a_spelt_out_url() -> None:
    assert find_upgrade_product(ENGINE_LITE) == ("swoslite", "css610pi")


def test_upgrade_product_from_a_url_built_at_runtime() -> None:
    # SwOS assembles the URL from a variable holding the product code.
    source = (
        'ba="css310g";let d="http://upgrade.mikrotik.com/swos2/"+ba.toLowerCase()+"/";'
    )
    assert find_upgrade_product(source) == ("swos2", "css310g")


def test_upgrade_product_absent() -> None:
    assert find_upgrade_product("function Ab(a){}") is None


@pytest.mark.parametrize(
    ("latest", "installed", "expected"),
    [
        ("2.22.1700000000", "2.21.1690000000", True),
        ("2.21.1766066090", "2.21.1766066090", False),
        # A rebuild of the same version is still an upgrade, and a switch
        # running something newer than the published build is not behind.
        ("2.21.1766066090", "2.21.1700000000", True),
        ("2.21.1700000000", "2.21.1766066090", False),
        ("2.9.1700000000", "2.13.1600000000", False),
    ],
)
def test_version_comparison(latest: str, installed: str, expected: bool) -> None:
    assert is_newer(latest, installed) is expected


async def test_firmware_update_pending(make_session) -> None:
    routes = dict(LITE_ROUTES)
    routes[("GET", "/swoslite/css610pi/LATEST")] = Raw("2.22.1800000000\n")
    snapshot = await make_client(make_session(routes)).async_fetch(slow=True)

    update = snapshot.updates["firmware"]
    assert (update.installed_version, update.latest_version) == ("2.21", "2.22")
    assert snapshot.pending_updates() == [update]
    assert update.attributes["product"] == "css610pi"
    assert update.release_url.endswith("/swoslite/css610pi/CHANGELOG")
    # Read-only: flashing a switch is not something a monitor should offer.
    assert update.install is None


async def test_firmware_up_to_date(make_session) -> None:
    routes = dict(LITE_ROUTES)
    routes[("GET", "/swoslite/css610pi/LATEST")] = Raw("2.21.1766066090")
    snapshot = await make_client(make_session(routes)).async_fetch(slow=True)

    update = snapshot.updates["firmware"]
    assert update.installed_version == update.latest_version == "2.21"
    assert snapshot.pending_updates() == []


async def test_a_rebuild_of_the_same_version_shows_both_builds(make_session) -> None:
    routes = dict(LITE_ROUTES)
    routes[("GET", "/swoslite/css610pi/LATEST")] = Raw("2.21.1800000000")
    snapshot = await make_client(make_session(routes)).async_fetch(slow=True)

    update = snapshot.updates["firmware"]
    # "2.21 → 2.21" would read as a bug, so the builds are shown instead.
    assert update.installed_version == "2.21.1766066090"
    assert update.latest_version == "2.21.1800000000"


async def test_no_internet_reports_the_installed_version(make_session) -> None:
    # No route for LATEST: the fake session answers 404, as an add-on with no
    # route out would fail to reach MikroTik at all.
    snapshot = await make_client(make_session(dict(LITE_ROUTES))).async_fetch(slow=True)
    update = snapshot.updates["firmware"]
    assert update.installed_version == update.latest_version == "2.21"


async def test_the_upgrade_server_never_sees_the_credentials(make_session) -> None:
    routes = dict(LITE_ROUTES)
    seen: list[str] = []

    def latest(headers: dict[str, str]):
        seen.append(headers.get("Authorization", ""))
        return Raw("2.22.1800000000")

    routes[("GET", "/swoslite/css610pi/LATEST")] = latest
    await make_client(make_session(routes)).async_fetch(slow=True)
    assert seen == [""]


async def test_firmware_check_can_be_turned_off(make_session) -> None:
    session = make_session(dict(LITE_ROUTES))
    await make_client(session, check_firmware=False).async_fetch(slow=True)
    assert not [call for call in session.calls if "LATEST" in call[1]]


# -- SwOS ----------------------------------------------------------------


@pytest.fixture
async def full(make_session):
    routes = digest_routes(
        {
            "/sys.b": SYS_FULL,
            "/link.b": LINK_FULL,
            "/stats.b": STATS_FULL,
        }
    )
    session = make_session(routes)
    return await make_client(session, check_firmware=False).async_fetch(slow=False)


async def test_swos_dialect_is_recognised(full) -> None:
    device = full.devices["main"]
    assert (device.name, device.model) == ("core-switch", "CRS310-8G+2S+")
    assert (device.sw_version, device.hw_version) == ("2.17", "r1.0")
    assert full.sensors["version"].attributes["firmware"] == "SwOS"


async def test_swos_uptime_is_in_hundredths_of_a_second(full) -> None:
    # The same raw uptime as the SwOS Lite fixture, which counts seconds.
    lite_boot = 0x0011375A
    boot = full.sensors["last_boot"].value
    assert boot is not None
    from datetime import UTC, datetime

    elapsed = (datetime.now(UTC) - boot).total_seconds()
    assert lite_boot / 100 - 60 < elapsed < lite_boot / 100 + 60


async def test_swos_health_and_ports(full) -> None:
    assert full.sensors["cpu_temperature"].value == 47
    assert full.sensors["board_temperature"].value == 37
    assert full.sensors["fan1"].value == 1800
    # p1s reports ok and p2s reports failed.
    assert full.binary_sensors["psu1_state"].value is False
    assert full.binary_sensors["psu2_state"].value is True
    assert full.binary_sensors["psu2_state"].reason == "PSU 2 reports failed"

    assert full.binary_sensors["port1_link"].name == "ether1 link"
    assert full.sensors["port1_speed"].value == "1G"
    assert full.sensors["port2_speed"].value == "10G"
    # 0x0003 link with 0x0002 paused: port 2 is up but flow-controlled.
    assert full.binary_sensors["port2_link"].attributes["paused"] is True
    assert full.sensors["port2_rx_bytes"].value == 2**32 + 0xE0A4396B
