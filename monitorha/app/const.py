"""Home-Assistant-shaped constants, without importing Home Assistant.

The add-on runs in its own container and must not depend on the `homeassistant`
package. These enums deliberately mirror the names and *values* of the real HA
enums, so the backend modules read identically to an HA integration and the
values serialise straight onto the wire. The integration on the other side maps
them back onto the genuine HA enums.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum


class SensorDeviceClass(StrEnum):
    TEMPERATURE = "temperature"
    VOLTAGE = "voltage"
    CURRENT = "current"
    POWER = "power"
    ENERGY = "energy"
    FREQUENCY = "frequency"
    DATA_SIZE = "data_size"
    TIMESTAMP = "timestamp"
    ENUM = "enum"


class SensorStateClass(StrEnum):
    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


class BinarySensorDeviceClass(StrEnum):
    PROBLEM = "problem"
    CONNECTIVITY = "connectivity"
    RUNNING = "running"
    UPDATE = "update"


class ButtonDeviceClass(StrEnum):
    RESTART = "restart"
    IDENTIFY = "identify"


class SwitchDeviceClass(StrEnum):
    OUTLET = "outlet"
    SWITCH = "switch"


class EntityCategory(StrEnum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


# Kept in step with config.yaml and the integration's manifest.json by a test.
# The Dockerfile copies only `app/` into the image, so the add-on cannot read
# its own config.yaml at runtime and has to carry the number itself.
VERSION = "0.11.1"

PERCENTAGE = "%"
REVOLUTIONS_PER_MINUTE = "rpm"


class UnitOfTemperature(StrEnum):
    CELSIUS = "°C"


class UnitOfElectricPotential(StrEnum):
    VOLT = "V"


class UnitOfElectricCurrent(StrEnum):
    AMPERE = "A"


class UnitOfPower(StrEnum):
    WATT = "W"


class UnitOfInformation(StrEnum):
    BYTES = "B"


class UnitOfFrequency(StrEnum):
    HERTZ = "Hz"


# -- source types ---------------------------------------------------------

TYPE_MIKROTIK = "mikrotik"
TYPE_PROXMOX = "proxmox"
TYPE_REDFISH = "redfish"
# MikroTik's switch firmware, which shares nothing with RouterOS but the vendor.
TYPE_SWOS = "swos"
SOURCE_TYPES = (TYPE_MIKROTIK, TYPE_PROXMOX, TYPE_REDFISH, TYPE_SWOS)

AUTH_TOKEN = "token"
AUTH_PASSWORD = "password"

POWER_OFF_GRACEFUL = "graceful"
POWER_OFF_FORCE = "force"

MAIN = "main"

DEFAULT_TIMEOUT = 20
DEFAULT_SLOW_SCAN_INTERVAL = 900
# SwOS serves its web UI over plain HTTP and has no HTTPS service to enable.
DEFAULT_PORTS = {
    TYPE_MIKROTIK: 443,
    TYPE_PROXMOX: 8006,
    TYPE_REDFISH: 443,
    TYPE_SWOS: 80,
}
# BMCs are underpowered and respond slowly, so they get a gentler default.
DEFAULT_SCAN_INTERVALS = {
    TYPE_MIKROTIK: 60,
    TYPE_PROXMOX: 60,
    TYPE_REDFISH: 120,
    TYPE_SWOS: 60,
}


_SLUG_STRIP = re.compile(r"[^a-z0-9_]+")
_SLUG_COLLAPSE = re.compile(r"_+")


def slugify(text: str) -> str:
    """Lowercase ASCII slug.

    Matches `homeassistant.util.slugify` closely enough that entity keys stay
    stable across the add-on and the integration.
    """
    normalised = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore")
    lowered = normalised.decode("ascii").lower()
    return _SLUG_COLLAPSE.sub("_", _SLUG_STRIP.sub("_", lowered)).strip("_")
