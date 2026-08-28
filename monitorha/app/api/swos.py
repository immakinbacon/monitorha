"""MikroTik SwOS and SwOS Lite backend.

The switch firmware has no REST API. Its web UI reads a handful of `.b` files
over HTTP with digest authentication, each one a JavaScript object literal
whose numbers are hex and whose strings are hex-encoded ASCII:

    {i01:0x03ff,i0a:['506f727431','506f727432'],i06:0x03f7}

That is the whole interface the firmware offers, so it is the one used here.
Two dialects exist and both are handled:

* **SwOS** (CRS/CSS3xx) names its fields mnemonically — `id`, `ver`, `lnk`.
* **SwOS Lite** (CSS1xx/CSS6xx) numbers them — `i05`, `i06`, `i08`.

`sys.b` tells them apart, and `_DIALECTS` maps each one onto the same set of
logical names. The field meanings, their scaling and the option lists all come
from the switch's own web UI, which carries a definition of every page.

Nothing here writes to the switch except the reboot button: applying a change
means POSTing a whole endpoint back, and a monitor has no business rewriting a
switch's port configuration to do its job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from ..const import (
    MAIN,
    REVOLUTIONS_PER_MINUTE,
    BinarySensorDeviceClass,
    ButtonDeviceClass,
    EntityCategory,
    SensorDeviceClass,
    SensorStateClass,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTemperature,
)
from ..models import (
    BinaryReading,
    ButtonSpec,
    DeviceMeta,
    Reading,
    Snapshot,
    UpdateReading,
)
from .base import AuthenticationError, BaseClient, ConnectionFailed

_LOGGER = logging.getLogger(__name__)

# -- wire format ---------------------------------------------------------

_HEX_NUMBER = re.compile(r"0x([0-9a-fA-F]+)")
_BARE_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:")


def parse_swos(text: str) -> Any:
    """Turn a `.b` response into plain Python.

    The body is JSON apart from three things: unquoted keys, hex numbers, and
    single-quoted strings. Rewriting those is safe in one pass because every
    quoted value is hex-encoded — it can contain neither a colon nor a quote,
    so neither substitution can reach inside one.
    """
    body = text.strip()
    if not body.startswith(("{", "[")):
        raise ConnectionFailed(
            "Unexpected response from SwOS: expected a configuration object, "
            f"got {body[:60]!r}"
        )
    converted = _HEX_NUMBER.sub(lambda m: str(int(m.group(1), 16)), body)
    converted = _BARE_KEY.sub(r'"\1":', converted).replace("'", '"')
    try:
        return json.loads(converted)
    except ValueError as err:
        raise ConnectionFailed(f"Could not parse the SwOS response: {err}") from err


def hex_text(value: Any) -> str | None:
    """Decode a hex-encoded string field. SwOS pads several of them."""
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = bytes.fromhex(value).decode("utf-8", errors="replace")
    except ValueError:
        return None
    # Trailing NULs and spaces are how SFP modules pad their SFF-8472 fields.
    return decoded.replace("\x00", "").strip() or None


def hex_mac(value: Any) -> str | None:
    text = value if isinstance(value, str) else None
    if not text or len(text) != 12:
        return None
    return ":".join(text[i : i + 2] for i in range(0, 12, 2)).upper()


def little_endian_ip(value: Any) -> str | None:
    """Decode an address field. SwOS stores the first octet in the low byte."""
    if not isinstance(value, int) or value == 0:
        return None
    return ".".join(str((value >> shift) & 0xFF) for shift in (0, 8, 16, 24))


def signed(value: Any, bits: int = 16) -> int | None:
    """Reinterpret an unsigned field as two's complement."""
    if not isinstance(value, int):
        return None
    limit = 1 << bits
    return value - limit if value >= limit // 2 else value


def bit(mask: Any, index: int) -> bool:
    """Read one port's bit out of a bitmask. Bit 0 is port 1."""
    return bool(isinstance(mask, int) and mask & (1 << index))


def element(values: Any, index: int) -> Any:
    """One port's entry from a per-port array, or None if it is absent.

    Several arrays are shorter than the port count — PoE fields cover only the
    powered ports on some models — so an index past the end is normal.
    """
    if isinstance(values, list) and 0 <= index < len(values):
        return values[index]
    return None


def wide(low: Any, high: Any) -> int | None:
    """Recombine a counter that the firmware splits into two 32-bit halves."""
    if not isinstance(low, int):
        return None
    return low + ((high << 32) if isinstance(high, int) else 0)


# -- dialects ------------------------------------------------------------

# Negotiated link speed, indexed by the code in the link table. A code past
# the end of this list is how the firmware spells "no link".
SPEEDS = ("10M", "100M", "1G", "10G", "200M", "2.5G", "5G")

# PoE status, indexed by the code in the PoE table. Index 0 means the port has
# no PoE hardware at all, which is why it is blank rather than a state.
POE_STATUS = (
    "",
    "disabled",
    "waiting for load",
    "powered on",
    "overload",
    "short circuit",
    "voltage too low",
    "current too low",
    "power cycle",
    "voltage too high",
    "controller error",
)

# The states that mean a powered port is actually in trouble. "waiting for
# load" is an empty port and "power cycle" is a transient, so neither counts.
POE_FAULTS = frozenset(
    {
        "overload",
        "short circuit",
        "voltage too low",
        "current too low",
        "voltage too high",
        "controller error",
    }
)

# An SFP with no diagnostics reports this instead of a temperature.
SFP_NO_READING = -128


@dataclass(frozen=True, slots=True)
class Dialect:
    """Field names for one firmware family, keyed by what they mean."""

    name: str
    """Human-readable firmware family, used as the model prefix."""
    uptime_divisor: int
    """SwOS counts uptime in hundredths of a second; SwOS Lite in seconds."""
    sys: dict[str, str]
    link: dict[str, str]
    poe: dict[str, str]
    sfp: dict[str, str]
    stats: dict[str, str]


SWOS = Dialect(
    name="SwOS",
    uptime_divisor=100,
    sys={
        "uptime": "upt",
        "ip": "cip",
        "mac": "mac",
        "serial": "sid",
        "identity": "id",
        "version": "ver",
        "build": "bld",
        "model": "brd",
        "board": "mrkt",
        "revision": "rev",
        "cpu_temperature": "temp",
        "board_temperature": "btm1",
        "board_temperature2": "btm2",
        "phy_temperature": "phyt",
        "fan1": "fan1",
        "fan2": "fan2",
        "fan3": "fan3",
        "fan4": "fan4",
        "psu1_voltage": "p1v",
        "psu1_current": "p1c",
        "psu2_voltage": "p2v",
        "psu2_current": "p2c",
        "psu1_state": "p1s",
        "psu2_state": "p2s",
    },
    link={
        "count": "prt",
        "sfp_count": "sfp",
        "enabled": "en",
        "names": "nm",
        "link": "lnk",
        "paused": "paus",
        "speed": "spd",
        "duplex": "dpx",
    },
    poe={
        "mode": "poe",
        "status": "poes",
        "current": "curr",
        "voltage": "volt",
        "power": "pwr",
        "priority": "prio",
    },
    sfp={
        "vendor": "vnd",
        "part": "pnr",
        "revision": "rev",
        "serial": "ser",
        "date": "dat",
        "type": "typ",
        "temperature": "tmp",
        "voltage": "vcc",
        "tx_bias": "tbs",
        "tx_power": "tpw",
        "rx_power": "rpw",
    },
    stats={
        "rx_bytes": "rb",
        "rx_bytes_high": "rbh",
        "tx_bytes": "tb",
        "tx_bytes_high": "tbh",
    },
)

SWOS_LITE = Dialect(
    name="SwOS Lite",
    uptime_divisor=1,
    sys={
        "uptime": "i01",
        "ip": "i02",
        "mac": "i03",
        "serial": "i04",
        "identity": "i05",
        "version": "i06",
        "build": "i0b",
        "model": "i07",
        "cpu_temperature": "i22",
        "psu1_voltage": "i15",
        "psu1_current": "i16",
        "psu2_voltage": "i1e",
        "psu2_current": "i1f",
        "power": "i26",
    },
    link={
        "enabled": "i01",
        "names": "i0a",
        "link": "i06",
        "paused": "i15",
        "speed": "i08",
        "duplex": "i07",
        "uptime": "i09",
    },
    poe={
        "mode": "i01",
        "status": "i04",
        "current": "i05",
        "voltage": "i06",
        "power": "i07",
        "priority": "i02",
    },
    sfp={
        "vendor": "i01",
        "part": "i02",
        "revision": "i03",
        "serial": "i04",
        "date": "i05",
        "type": "i06",
        "temperature": "i08",
        "voltage": "i09",
        "tx_bias": "i0a",
        "tx_power": "i0b",
        "rx_power": "i0c",
    },
    stats={
        "rx_bytes": "i01",
        "rx_bytes_high": "i02",
        "tx_bytes": "i0f",
        "tx_bytes_high": "i10",
    },
)


def detect_dialect(system: dict[str, Any]) -> Dialect:
    """Tell the two field-naming schemes apart from a `sys.b` response."""
    if any(key in system for key in ("ver", "brd", "id")):
        return SWOS
    return SWOS_LITE


# -- firmware updates ----------------------------------------------------

# SwOS cannot check for its own updates: the *browser* fetches MikroTik's
# version file, and the product code it uses is baked into each firmware's web
# UI. Reading that code back out of the UI is what makes the check work on any
# model rather than only on the ones somebody thought to list here.
UPGRADE_HOST = "http://upgrade.mikrotik.com"
_UPGRADE_LITERAL = re.compile(
    r"upgrade\.mikrotik\.com/(swos2|swoslite)/([A-Za-z0-9+._-]+)/"
)
_UPGRADE_BUILT = re.compile(
    r"upgrade\.mikrotik\.com/(swos2|swoslite)/\"\s*\+\s*(\w+)\.toLowerCase\(\)"
)
# Where the web UI lives, newest firmware first.
_UI_PATHS = ("engine.js", "index.html")


def find_upgrade_product(source: str) -> tuple[str, str] | None:
    """Pull the (channel, product) pair out of a switch's web UI source.

    SwOS Lite spells the URL out; SwOS builds it from a variable holding the
    product code, so both spellings are looked for.
    """
    literal = _UPGRADE_LITERAL.search(source)
    if literal:
        return literal.group(1), literal.group(2).lower()

    built = _UPGRADE_BUILT.search(source)
    if built:
        assigned = re.search(
            rf"\b{re.escape(built.group(2))}\s*=\s*\"([A-Za-z0-9+._-]+)\"", source
        )
        if assigned:
            return built.group(1), assigned.group(1).lower()
    return None


def version_parts(version: str) -> tuple[int, ...]:
    """Split a `2.21.1766066090` version into comparable numbers."""
    parts = []
    for chunk in str(version).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, installed: str) -> bool:
    """True when MikroTik publishes something ahead of what is installed.

    The published version carries the build timestamp as a final component, so
    a rebuild of the same version number still compares as newer — which is
    exactly how the switch's own upgrade page treats it.
    """
    left, right = version_parts(latest), version_parts(installed)
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left > right


# -- digest authentication -----------------------------------------------

_HASHES = {
    "MD5": hashlib.md5,
    "MD5-SESS": hashlib.md5,
    "SHA-256": hashlib.sha256,
    "SHA-256-SESS": hashlib.sha256,
}
_CHALLENGE_FIELD = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s,]+))')


class DigestAuth:
    """The digest half of RFC 7616, which is all SwOS speaks.

    aiohttp only ships Basic authentication, and SwOS answers a Basic request
    with 401 forever, so the challenge/response has to be done here.
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._challenge: dict[str, str] = {}
        self._nonce_count = 0

    @property
    def ready(self) -> bool:
        return bool(self._challenge)

    def take_challenge(self, header: str | None) -> bool:
        """Absorb a `WWW-Authenticate` header. False if it is not digest."""
        if not header or not header.strip().lower().startswith("digest"):
            return False
        fields = {
            match.group(1).lower(): match.group(2) or match.group(3) or ""
            for match in _CHALLENGE_FIELD.finditer(header)
        }
        if "nonce" not in fields:
            return False
        self._challenge = fields
        self._nonce_count = 0
        return True

    def authorization(self, method: str, uri: str) -> str | None:
        """Build the `Authorization` header for one request."""
        if not self._challenge:
            return None
        realm = self._challenge.get("realm", "")
        nonce = self._challenge["nonce"]
        algorithm = self._challenge.get("algorithm", "MD5").upper()
        digest = _HASHES.get(algorithm)
        if digest is None:
            raise AuthenticationError(
                f"Unsupported digest algorithm {algorithm!r} offered by {realm!r}"
            )

        def h(text: str) -> str:
            return digest(text.encode("utf-8")).hexdigest()

        ha1 = h(f"{self._username}:{realm}:{self._password}")
        ha2 = h(f"{method}:{uri}")
        # A quoted qop list may offer several; only "auth" is implemented, and
        # a server that offers only auth-int gets an unqualified response,
        # which it will reject clearly rather than silently mis-authenticating.
        qop = "auth" if "auth" in self._challenge.get("qop", "").split(",") else ""

        parts = [
            f'username="{self._username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
        ]
        if qop:
            self._nonce_count += 1
            count = f"{self._nonce_count:08x}"
            cnonce = uuid4().hex[:16]
            if algorithm.endswith("-SESS"):
                ha1 = h(f"{ha1}:{nonce}:{cnonce}")
            response = h(f"{ha1}:{nonce}:{count}:{cnonce}:{qop}:{ha2}")
            parts += [f"qop={qop}", f"nc={count}", f'cnonce="{cnonce}"']
        else:
            response = h(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{response}"')

        if "opaque" in self._challenge:
            parts.append(f'opaque="{self._challenge["opaque"]}"')
        if algorithm != "MD5":
            parts.append(f"algorithm={algorithm}")
        return "Digest " + ", ".join(parts)


class SwosClient(BaseClient):
    """SwOS / SwOS Lite client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_ssl: bool = False,
        monitor_ports: bool = True,
        monitor_poe: bool = True,
        monitor_sfp: bool = True,
        check_firmware: bool = True,
    ) -> None:
        super().__init__(session, host, port, use_ssl=use_ssl)
        self._auth = DigestAuth(username, password)
        self._monitor_ports = monitor_ports
        self._monitor_poe = monitor_poe
        self._monitor_sfp = monitor_sfp
        self._check_firmware = check_firmware
        self._dialect: Dialect = SWOS_LITE
        # Discovered once from the web UI; None until looked for, and False
        # once a look has failed, so a switch whose UI does not name a product
        # is not re-read on every deep poll.
        self._product: tuple[str, str] | None | bool = None
        self._latest: str | None = None
        self._firmware_checked = False
        self._sfp_cages: int | None = None

    # -- transport --------------------------------------------------------

    async def _http(
        self,
        method: str,
        url: str,
        *,
        body: str | None = None,
        optional: bool = False,
        authenticate: bool = True,
    ) -> str | None:
        """Fetch a URL as text, answering a digest challenge if one arrives.

        Returns None when `optional` and the switch does not have the
        endpoint. SwOS answers an unsupported path with a redirect to its index
        page rather than a 404, so redirects are not followed: doing so would
        turn "no such endpoint" into a page of HTML that parses as garbage.
        """
        # The digest response signs the request-target, which is the path.
        path = urlsplit(url).path or "/"
        headers = {"Content-Type": "text/plain"} if body is not None else {}

        for attempt in range(2):
            if authenticate and self._auth.ready:
                authorization = self._auth.authorization(method, path)
                if authorization:
                    headers["Authorization"] = authorization
            try:
                response = await self._session.request(
                    method,
                    url,
                    headers=headers or None,
                    data=body,
                    timeout=self._timeout,
                    allow_redirects=False,
                )
            except aiohttp.ClientConnectorCertificateError as err:
                raise ConnectionFailed(
                    f"TLS certificate rejected for {url}. Disable 'Verify SSL "
                    f"certificate' if this switch uses a self-signed "
                    f"certificate: {err}"
                ) from err
            except aiohttp.ClientError as err:
                if optional:
                    _LOGGER.debug("Skipping optional %s: %s", url, err)
                    return None
                raise ConnectionFailed(f"Error connecting to {url}: {err}") from err
            except TimeoutError as err:
                if optional:
                    _LOGGER.debug("Timeout on optional %s, skipping", url)
                    return None
                raise ConnectionFailed(
                    f"Timeout after {self._timeout.total}s connecting to {url}"
                ) from err

            async with response:
                # The first request of a session is always unauthenticated:
                # the nonce to answer with only arrives with this refusal.
                if (
                    response.status == 401
                    and attempt == 0
                    and authenticate
                    and self._auth.take_challenge(
                        response.headers.get("WWW-Authenticate")
                    )
                ):
                    continue
                if response.status == 401:
                    raise AuthenticationError(
                        f"Credentials rejected by {url} (HTTP 401). SwOS uses "
                        f"the same user as its web interface."
                    )
                if response.status in (403, 404) or 300 <= response.status < 400:
                    if optional:
                        _LOGGER.debug(
                            "%s answered HTTP %s; treating as unsupported",
                            url,
                            response.status,
                        )
                        return None
                    raise ConnectionFailed(
                        f"HTTP {response.status} from {url}. Is this a SwOS "
                        f"switch, and is its web service reachable?"
                    )
                if response.status >= 400:
                    raise ConnectionFailed(f"HTTP {response.status} from {url}")
                return await response.text()

        raise ConnectionFailed(f"Could not authenticate to {url}")

    async def _read(self, path: str, *, optional: bool = False) -> Any:
        """GET one `.b` endpoint and decode it."""
        text = await self._http("GET", f"{self.base_url}/{path}", optional=optional)
        if text is None:
            return None
        if optional and not text.strip().startswith(("{", "[")):
            # Some models answer an endpoint they do not implement with an
            # empty body or a scrap of HTML instead of a redirect.
            _LOGGER.debug("Ignoring non-object body from %s", path)
            return None
        return parse_swos(text)

    async def _reboot(self) -> None:
        """The web UI's reboot: a POST whose body the firmware ignores."""
        await self._http("POST", f"{self.base_url}/reboot", body="*")

    # -- polling ----------------------------------------------------------

    async def async_validate(self) -> dict[str, Any]:
        system = await self._read("sys.b")
        if not isinstance(system, dict):
            raise ConnectionFailed(
                "Unexpected response from /sys.b — is this a MikroTik switch "
                "running SwOS or SwOS Lite?"
            )
        dialect = detect_dialect(system)
        fields = dialect.sys
        serial = hex_text(system.get(fields["serial"]))
        identity = hex_text(system.get(fields["identity"]))
        model = hex_text(system.get(fields["model"]))
        return {
            "unique_id": serial or f"{self._host}:{self._port}",
            "title": identity or model or self._host,
            "model": model,
            "firmware": dialect.name,
        }

    async def async_fetch(self, *, slow: bool) -> Snapshot:
        snapshot = Snapshot()

        system = await self._read("sys.b")
        if not isinstance(system, dict):
            raise ConnectionFailed("SwOS returned no system data from /sys.b")
        self._dialect = detect_dialect(system)

        names = self._add_system(snapshot, system)
        link = await self._read("link.b", optional=True)
        ports = self._add_ports(snapshot, link if isinstance(link, dict) else {})
        if self._monitor_ports:
            await self._add_statistics(snapshot, ports)
        if self._monitor_poe:
            await self._add_poe(snapshot, ports)
        if self._monitor_sfp:
            await self._add_sfp(snapshot, ports)
        await self._add_firmware(snapshot, system, slow=slow)

        snapshot.add_button(
            ButtonSpec(
                key="reboot",
                name="Reboot",
                press=self._reboot,
                device_class=ButtonDeviceClass.RESTART,
                entity_category=EntityCategory.CONFIG,
                enabled_default=False,
            )
        )
        _LOGGER.debug("Polled %s (%s)", names, self._dialect.name)
        return snapshot

    # -- sections ---------------------------------------------------------

    def _sys(self, system: dict[str, Any], name: str) -> Any:
        """One logical system field, or None if this dialect lacks it."""
        key = self._dialect.sys.get(name)
        return None if key is None else system.get(key)

    def _add_system(self, snapshot: Snapshot, system: dict[str, Any]) -> str:
        identity = hex_text(self._sys(system, "identity"))
        model = hex_text(self._sys(system, "model"))
        version = hex_text(self._sys(system, "version"))
        build = self._sys(system, "build")
        name = identity or model or self._host

        snapshot.add_device(
            DeviceMeta(
                key=MAIN,
                name=name,
                manufacturer="MikroTik",
                model=model,
                sw_version=version,
                hw_version=hex_text(self._sys(system, "revision")),
                serial_number=hex_text(self._sys(system, "serial")),
                configuration_url=self.base_url,
            )
        )

        uptime = self._sys(system, "uptime")
        if isinstance(uptime, int):
            snapshot.add(
                Reading(
                    key="last_boot",
                    name="Last boot",
                    value=self.boot_time(MAIN, uptime / self._dialect.uptime_divisor),
                    device_class=SensorDeviceClass.TIMESTAMP,
                    entity_category=EntityCategory.DIAGNOSTIC,
                )
            )

        snapshot.add(
            Reading(
                key="version",
                name="SwOS version",
                value=version,
                entity_category=EntityCategory.DIAGNOSTIC,
                icon="mdi:package-variant",
                attributes={
                    "firmware": self._dialect.name,
                    "board_name": hex_text(self._sys(system, "board")),
                    "build_time": _build_time(build),
                    "ip_address": little_endian_ip(self._sys(system, "ip")),
                    "mac_address": hex_mac(self._sys(system, "mac")),
                },
            )
        )

        for key, field, label in (
            ("cpu_temperature", "cpu_temperature", "CPU temperature"),
            ("board_temperature", "board_temperature", "Board temperature"),
            ("board_temperature2", "board_temperature2", "Board temperature 2"),
            ("phy_temperature", "phy_temperature", "PHY temperature"),
        ):
            value = signed(self._sys(system, field))
            if value is None:
                continue
            snapshot.add(
                Reading(
                    key=key,
                    name=label,
                    value=value,
                    device_class=SensorDeviceClass.TEMPERATURE,
                    unit=UnitOfTemperature.CELSIUS,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                )
            )

        for index in (1, 2, 3, 4):
            rpm = self._sys(system, f"fan{index}")
            if not isinstance(rpm, int):
                continue
            snapshot.add(
                Reading(
                    key=f"fan{index}",
                    name=f"Fan {index}",
                    value=rpm,
                    unit=REVOLUTIONS_PER_MINUTE,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    icon="mdi:fan",
                )
            )

        self._add_power_supplies(snapshot, system)

        # Total draw, which on a PoE switch is mostly what the ports are
        # pulling: the one number worth putting on a dashboard.
        power = self._sys(system, "power")
        if isinstance(power, int):
            snapshot.add(
                Reading(
                    key="power_consumption",
                    name="Power consumption",
                    value=power / 10,
                    device_class=SensorDeviceClass.POWER,
                    unit=UnitOfPower.WATT,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=1,
                )
            )
        return name

    def _add_power_supplies(self, snapshot: Snapshot, system: dict[str, Any]) -> None:
        for index in (1, 2):
            voltage = self._sys(system, f"psu{index}_voltage")
            current = self._sys(system, f"psu{index}_current")
            state = self._sys(system, f"psu{index}_state")

            if isinstance(voltage, int):
                snapshot.add(
                    Reading(
                        key=f"psu{index}_voltage",
                        name=f"PSU {index} voltage",
                        value=voltage / 100,
                        device_class=SensorDeviceClass.VOLTAGE,
                        unit=UnitOfElectricPotential.VOLT,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=2,
                        entity_category=EntityCategory.DIAGNOSTIC,
                    )
                )
            if isinstance(current, int):
                snapshot.add(
                    Reading(
                        key=f"psu{index}_current",
                        name=f"PSU {index} current",
                        value=current / 1000,
                        device_class=SensorDeviceClass.CURRENT,
                        unit=UnitOfElectricCurrent.AMPERE,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=3,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )
            if isinstance(state, int):
                failed = state == 0
                snapshot.add_binary(
                    BinaryReading(
                        key=f"psu{index}_state",
                        name=f"PSU {index}",
                        value=failed,
                        reason=f"PSU {index} reports failed" if failed else None,
                        device_class=BinarySensorDeviceClass.PROBLEM,
                        entity_category=EntityCategory.DIAGNOSTIC,
                    )
                )

    def _add_ports(self, snapshot: Snapshot, link: dict[str, Any]) -> list[str]:
        """Per-port link state and speed. Returns the port names, in order."""
        fields = self._dialect.link
        raw_names = link.get(fields["names"])
        names: list[str] = []
        if isinstance(raw_names, list):
            names = [
                hex_text(entry) or f"Port {index + 1}"
                for index, entry in enumerate(raw_names)
            ]
        if not names:
            total = link.get(fields.get("count", ""))
            names = [
                f"Port {i + 1}" for i in range(total if isinstance(total, int) else 0)
            ]
        # SwOS states how many of the ports are SFP cages; SwOS Lite does not,
        # and the cages are always last, so that is what gets assumed there.
        cages = link.get(fields.get("sfp_count", ""))
        self._sfp_cages = cages if isinstance(cages, int) else None
        if not names or not self._monitor_ports:
            return names

        enabled_mask = link.get(fields["enabled"])
        link_mask = link.get(fields["link"])
        paused_mask = link.get(fields["paused"])
        speeds = link.get(fields["speed"])
        duplex_mask = link.get(fields["duplex"])
        uptimes = link.get(fields.get("uptime", ""))

        for index, port_name in enumerate(names):
            number = index + 1
            up = bit(link_mask, index)
            paused = up and bit(paused_mask, index)
            code = element(speeds, index)
            speed = SPEEDS[code] if isinstance(code, int) and code < len(SPEEDS) else None
            port_uptime = element(uptimes, index)

            snapshot.add_binary(
                BinaryReading(
                    key=f"port{number}_link",
                    name=f"{port_name} link",
                    value=up,
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={
                        "port": number,
                        "enabled": bit(enabled_mask, index),
                        "speed": speed,
                        "full_duplex": bit(duplex_mask, index) if up else None,
                        # A paused link is up but flow-controlled to a halt,
                        # which looks like a dead port from anywhere else.
                        "paused": paused,
                        "link_uptime_seconds": port_uptime,
                    },
                )
            )
            snapshot.add(
                Reading(
                    key=f"port{number}_speed",
                    name=f"{port_name} speed",
                    value=speed,
                    device_class=SensorDeviceClass.ENUM,
                    options=list(SPEEDS),
                    entity_category=EntityCategory.DIAGNOSTIC,
                    icon="mdi:speedometer",
                )
            )
        return names

    async def _add_statistics(self, snapshot: Snapshot, ports: list[str]) -> None:
        """Per-port byte counters.

        The endpoint is `!stats.b` on SwOS Lite and `stats.b` on SwOS, and the
        leading bang has moved between firmware versions, so both are tried.
        """
        stats = None
        for path in ("!stats.b", "stats.b"):
            stats = await self._read(path, optional=True)
            if isinstance(stats, dict):
                break
        if not isinstance(stats, dict):
            return

        fields = self._dialect.stats
        for index, port_name in enumerate(ports):
            number = index + 1
            for direction, low_key, high_key in (
                ("rx", "rx_bytes", "rx_bytes_high"),
                ("tx", "tx_bytes", "tx_bytes_high"),
            ):
                # The counters are 64-bit, split across two 32-bit fields.
                total = wide(
                    element(stats.get(fields[low_key]), index),
                    element(stats.get(fields[high_key]), index),
                )
                if total is None:
                    continue
                snapshot.add(
                    Reading(
                        key=f"port{number}_{direction}_bytes",
                        name=f"{port_name} {direction.upper()}",
                        value=total,
                        device_class=SensorDeviceClass.DATA_SIZE,
                        unit=UnitOfInformation.BYTES,
                        # Lets Home Assistant derive throughput and survive the
                        # counter resetting when the switch reboots.
                        state_class=SensorStateClass.TOTAL_INCREASING,
                        suggested_display_precision=0,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )

    async def _add_poe(self, snapshot: Snapshot, ports: list[str]) -> None:
        poe = await self._read("poe.b", optional=True)
        if not isinstance(poe, dict):
            return
        fields = self._dialect.poe
        statuses = poe.get(fields["status"])
        if not isinstance(statuses, list):
            return

        for index, code in enumerate(statuses):
            if not isinstance(code, int) or code <= 0:
                # Index 0 means the port has no PoE hardware behind it.
                continue
            number = index + 1
            port_name = ports[index] if index < len(ports) else f"Port {number}"
            # An unrecognised code is reported as unknown rather than as a
            # made-up option, which Home Assistant would refuse to accept.
            status = POE_STATUS[code] if code < len(POE_STATUS) else None
            faulted = status in POE_FAULTS

            snapshot.add(
                Reading(
                    key=f"port{number}_poe_status",
                    name=f"{port_name} PoE status",
                    value=status,
                    device_class=SensorDeviceClass.ENUM,
                    options=[s for s in POE_STATUS if s],
                    icon="mdi:ethernet-cable",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={"raw_status": code},
                )
            )
            snapshot.add_binary(
                BinaryReading(
                    key=f"port{number}_poe_problem",
                    name=f"{port_name} PoE",
                    value=faulted,
                    reason=f"{port_name} PoE reports {status}" if faulted else None,
                    device_class=BinarySensorDeviceClass.PROBLEM,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes={"status": status},
                )
            )

            power = element(poe.get(fields["power"]), index)
            if isinstance(power, int):
                snapshot.add(
                    Reading(
                        key=f"port{number}_poe_power",
                        name=f"{port_name} PoE power",
                        value=power / 10,
                        device_class=SensorDeviceClass.POWER,
                        unit=UnitOfPower.WATT,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=1,
                    )
                )
            voltage = element(poe.get(fields["voltage"]), index)
            if isinstance(voltage, int):
                snapshot.add(
                    Reading(
                        key=f"port{number}_poe_voltage",
                        name=f"{port_name} PoE voltage",
                        value=voltage / 10,
                        device_class=SensorDeviceClass.VOLTAGE,
                        unit=UnitOfElectricPotential.VOLT,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=1,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )
            current = element(poe.get(fields["current"]), index)
            if isinstance(current, int):
                snapshot.add(
                    Reading(
                        key=f"port{number}_poe_current",
                        name=f"{port_name} PoE current",
                        value=current / 1000,
                        device_class=SensorDeviceClass.CURRENT,
                        unit=UnitOfElectricCurrent.AMPERE,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=3,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )

    async def _add_sfp(self, snapshot: Snapshot, ports: list[str]) -> None:
        sfp = await self._read("sfp.b", optional=True)
        if not isinstance(sfp, dict):
            return
        fields = self._dialect.sfp
        vendors = sfp.get(fields["vendor"])
        if not isinstance(vendors, list):
            # A single-cage switch reports one module as bare values.
            vendors = [vendors]
            sfp = {key: [value] for key, value in sfp.items()}

        for index in range(len(vendors)):
            module = index + 1
            name = self._sfp_name(index, len(vendors), ports)
            temperature = signed(element(sfp.get(fields["temperature"]), index))
            if temperature is None or temperature == SFP_NO_READING:
                # No diagnostics: an empty cage, or a passive DAC that reports
                # the sentinel rather than a reading.
                continue

            attributes = {
                "vendor": hex_text(element(sfp.get(fields["vendor"]), index)),
                "part_number": hex_text(element(sfp.get(fields["part"]), index)),
                "revision": hex_text(element(sfp.get(fields["revision"]), index)),
                "serial_number": hex_text(element(sfp.get(fields["serial"]), index)),
                "date": hex_text(element(sfp.get(fields["date"]), index)),
                "type": hex_text(element(sfp.get(fields["type"]), index)),
            }
            snapshot.add(
                Reading(
                    key=f"sfp{module}_temperature",
                    name=f"{name} temperature",
                    value=temperature,
                    device_class=SensorDeviceClass.TEMPERATURE,
                    unit=UnitOfTemperature.CELSIUS,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    attributes=attributes,
                )
            )

            voltage = element(sfp.get(fields["voltage"]), index)
            if isinstance(voltage, int):
                snapshot.add(
                    Reading(
                        key=f"sfp{module}_voltage",
                        name=f"{name} voltage",
                        value=voltage / 1000,
                        device_class=SensorDeviceClass.VOLTAGE,
                        unit=UnitOfElectricPotential.VOLT,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=2,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )
            bias = element(sfp.get(fields["tx_bias"]), index)
            if isinstance(bias, int):
                snapshot.add(
                    Reading(
                        key=f"sfp{module}_tx_bias",
                        name=f"{name} TX bias",
                        value=bias / 1000,
                        device_class=SensorDeviceClass.CURRENT,
                        unit=UnitOfElectricCurrent.AMPERE,
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=3,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        enabled_default=False,
                    )
                )
            for key, label in (("tx_power", "TX power"), ("rx_power", "RX power")):
                power = _dbm(element(sfp.get(fields[key]), index))
                if power is None:
                    continue
                snapshot.add(
                    Reading(
                        key=f"sfp{module}_{key}",
                        name=f"{name} {label}",
                        value=power,
                        unit="dBm",
                        state_class=SensorStateClass.MEASUREMENT,
                        suggested_display_precision=2,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        icon="mdi:light-flood-down",
                    )
                )

    def _sfp_name(self, index: int, total: int, ports: list[str]) -> str:
        """Name an SFP cage after its port, which is what people call it.

        The cages are the last ports on every model that has them; SwOS says so
        outright with a cage count, and SwOS Lite leaves it to be inferred.
        """
        cages = self._sfp_cages or total
        position = len(ports) - cages + index
        if 0 <= position < len(ports):
            return ports[position]
        return f"SFP {index + 1}"

    # -- firmware ---------------------------------------------------------

    async def _add_firmware(
        self, snapshot: Snapshot, system: dict[str, Any], *, slow: bool
    ) -> None:
        """Publish the installed firmware, and what MikroTik has published.

        The switch never checks for its own updates — its web UI asks
        MikroTik's server from the browser — so the same request is made here,
        on the deep poll only, and a switch with no route to the internet
        simply reports its installed version.
        """
        version = hex_text(self._sys(system, "version"))
        if not version:
            return
        build = self._sys(system, "build")
        installed = f"{version}.{build}" if isinstance(build, int) else version

        if self._check_firmware and (slow or not self._firmware_checked):
            # A switch with no route to the internet must not turn every poll
            # into a doomed outbound request, so one attempt per deep poll is
            # all it gets, successful or not.
            self._firmware_checked = True
            await self._refresh_latest()

        latest = self._latest
        pending = bool(latest and is_newer(latest, installed))
        product = self._product if isinstance(self._product, tuple) else None

        installed_display = version
        latest_display = version
        if pending and latest:
            pretty = latest.rsplit(".", 1)[0] if "." in latest else latest
            # A rebuild of the same version number is still an upgrade, but
            # "2.21 → 2.21" reads as a bug; show the builds when that happens.
            installed_display, latest_display = (
                (installed, latest) if pretty == version else (version, pretty)
            )

        snapshot.add_update(
            UpdateReading(
                key="firmware",
                name=f"{self._dialect.name} firmware",
                installed_version=installed_display,
                latest_version=latest_display,
                title=self._dialect.name,
                release_url=(
                    f"{UPGRADE_HOST}/{product[0]}/{product[1]}/CHANGELOG"
                    if product
                    else None
                ),
                attributes={
                    "build_time": _build_time(build),
                    "product": product[1] if product else None,
                    "channel": product[0] if product else None,
                    "published_version": latest,
                },
            )
        )

    async def _refresh_latest(self) -> None:
        """Ask MikroTik what it publishes for this switch, if anything."""
        product = await self._upgrade_product()
        if product is None:
            return
        channel, code = product
        text = await self._http(
            "GET",
            f"{UPGRADE_HOST}/{channel}/{code}/LATEST",
            optional=True,
            # A third-party host must never see the switch's credentials.
            authenticate=False,
        )
        if text and text.strip():
            self._latest = text.strip().split()[0]

    async def _upgrade_product(self) -> tuple[str, str] | None:
        """The product code MikroTik publishes this switch's firmware under.

        It is not derivable from the model — a CRS310-8G+2S+ is published as
        `css310g` — but each firmware's own web UI contains the URL it would
        use, so it is read back from there and then remembered.
        """
        if self._product is not None:
            return self._product if isinstance(self._product, tuple) else None

        for path in _UI_PATHS:
            source = await self._http(
                "GET", f"{self.base_url}/{path}", optional=True
            )
            if not source:
                continue
            found = find_upgrade_product(source)
            if found:
                self._product = found
                _LOGGER.debug("%s publishes firmware as %s", self._host, found)
                return found

        _LOGGER.debug("No upgrade product found in the web UI of %s", self._host)
        self._product = False
        return None


def _build_time(build: Any) -> str | None:
    """SwOS stamps its builds with a Unix timestamp."""
    if not isinstance(build, int) or build <= 0:
        return None
    return datetime.fromtimestamp(build, UTC).isoformat()


def _dbm(raw: Any) -> float | None:
    """Optical power in dBm, from the SFP's tenths-of-a-microwatt reading.

    SFF-8472 reports power in units of 0.1 µW and SwOS passes that through
    untouched; its web UI is what applies the logarithm.
    """
    if not isinstance(raw, int) or raw <= 0:
        return None
    return round(10 * math.log10(raw / 10000), 3)


__all__ = [
    "SWOS",
    "SWOS_LITE",
    "DigestAuth",
    "SwosClient",
    "detect_dialect",
    "find_upgrade_product",
    "is_newer",
    "parse_swos",
]
