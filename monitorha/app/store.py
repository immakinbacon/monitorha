"""Persistent source configuration.

Devices are configured in the add-on's web UI, not in Home Assistant, so their
credentials live here in the add-on's `/data` volume rather than in a config
entry.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .const import (
    DEFAULT_PORTS,
    DEFAULT_SCAN_INTERVALS,
    DEFAULT_SLOW_SCAN_INTERVAL,
    SOURCE_TYPES,
)

_LOGGER = logging.getLogger(__name__)

# Never leave the add-on in an API response.
SECRET_FIELDS = ("password", "token_secret")

# Bounds a monitor line can carry. "above" fires when the value rises past the
# bound, "below" when it drops past it; a line may set any combination.
THRESHOLD_FIELDS = ("warn_above", "warn_below", "critical_above", "critical_below")


class ConfigError(ValueError):
    """A source definition was rejected."""


def split_host(value: str) -> tuple[str, int | None]:
    """Normalise whatever was typed into the Host field.

    The clients compose `https://{host}:{port}` themselves, so a pasted browser
    URL would otherwise end up doubled. Accepts a bare host, `host:port`, or a
    full URL, and returns the host plus any port found in it.
    """
    text = str(value).strip()
    if not text:
        return "", None

    if "://" in text:
        parsed = urlparse(text)
        return (parsed.hostname or "").strip(), parsed.port

    text = text.strip("/")
    if text.startswith("["):
        # Bracketed IPv6, optionally with a port: [::1]:8006
        host, _, rest = text.partition("]")
        port = rest.lstrip(":")
        return host.lstrip("["), int(port) if port.isdigit() else None
    # A single colon means host:port; several means a bare IPv6 address.
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            return host, int(port)
    return text, None


def validate_source(data: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalise and validate a source definition from the web UI."""
    source_type = str(data.get("type") or (existing or {}).get("type") or "")
    if source_type not in SOURCE_TYPES:
        raise ConfigError(f"Unknown source type: {source_type!r}")

    host, url_port = split_host(
        str(data.get("host") or (existing or {}).get("host") or "")
    )
    if not host:
        raise ConfigError("Host is required")

    merged: dict[str, Any] = dict(existing or {})
    merged.update({k: v for k, v in data.items() if v is not None})

    # An edit that leaves a secret field blank keeps the stored value, so the
    # UI never has to round-trip the password back to the browser.
    for field in SECRET_FIELDS:
        if not merged.get(field) and existing and existing.get(field):
            merged[field] = existing[field]

    result: dict[str, Any] = {
        "id": merged.get("id") or uuid.uuid4().hex,
        "type": source_type,
        "name": str(merged.get("name") or host).strip(),
        "host": host,
        # A port typed into the host field is the most explicit thing the user
        # said, so it beats the port box, which is prefilled with a default.
        "port": int(url_port or merged.get("port") or DEFAULT_PORTS[source_type]),
        "enabled": bool(merged.get("enabled", True)),
        "verify_ssl": bool(merged.get("verify_ssl", False)),
        "scan_interval": max(
            10,
            int(merged.get("scan_interval") or DEFAULT_SCAN_INTERVALS[source_type]),
        ),
        "slow_scan_interval": max(
            60, int(merged.get("slow_scan_interval") or DEFAULT_SLOW_SCAN_INTERVAL)
        ),
    }

    if source_type == "mikrotik":
        result.update(
            username=str(merged.get("username") or ""),
            password=str(merged.get("password") or ""),
            use_ssl=bool(merged.get("use_ssl", True)),
            monitor_interfaces=bool(merged.get("monitor_interfaces", True)),
            monitor_tunnels=bool(merged.get("monitor_tunnels", True)),
            monitor_netwatch=bool(merged.get("monitor_netwatch", True)),
        )
    elif source_type == "swos":
        result.update(
            username=str(merged.get("username") or ""),
            password=str(merged.get("password") or ""),
            # SwOS has no HTTPS service to turn on, so this defaults the other
            # way round from RouterOS.
            use_ssl=bool(merged.get("use_ssl", False)),
            monitor_ports=bool(merged.get("monitor_ports", True)),
            monitor_poe=bool(merged.get("monitor_poe", True)),
            monitor_sfp=bool(merged.get("monitor_sfp", True)),
            check_firmware=bool(merged.get("check_firmware", True)),
        )
        if not result["username"]:
            raise ConfigError("Username is required")
    elif source_type == "proxmox":
        auth_method = merged.get("auth_method") or "token"
        scope = str(merged.get("scope") or "node").strip().lower()
        if scope not in ("node", "cluster"):
            raise ConfigError("Scope must be either 'node' or 'cluster'")
        result.update(
            auth_method=auth_method,
            scope=scope,
            # Blank means the node we connect to, resolved from /cluster/status.
            node=str(merged.get("node") or "").strip(),
            monitor_guests=bool(merged.get("monitor_guests", True)),
            monitor_backups=bool(merged.get("monitor_backups", True)),
            monitor_interfaces=bool(merged.get("monitor_interfaces", True)),
        )
        if auth_method == "token":
            result.update(
                token_id=str(merged.get("token_id") or ""),
                token_secret=str(merged.get("token_secret") or ""),
            )
            if not result["token_id"]:
                raise ConfigError("Token ID is required")
        else:
            result.update(
                username=str(merged.get("username") or ""),
                password=str(merged.get("password") or ""),
            )
            if not result["username"]:
                raise ConfigError("Username is required")
    else:  # redfish
        result.update(
            username=str(merged.get("username") or ""),
            password=str(merged.get("password") or ""),
            power_off_action=merged.get("power_off_action") or "graceful",
        )
        if not result["username"]:
            raise ConfigError("Username is required")

    return result


def validate_override(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise one monitor line's per-line settings.

    `muted` suppresses events and drops the line from the problem rollup; it
    deliberately does not hide the entity, so muting and unmuting never churns
    the Home Assistant entity registry.
    """
    thresholds: dict[str, float] = {}
    raw = data.get("thresholds") or {}
    if not isinstance(raw, dict):
        raise ConfigError("Thresholds must be an object")
    for name in THRESHOLD_FIELDS:
        value = raw.get(name)
        # An empty box in the UI clears that bound rather than setting zero.
        if value is None or value == "":
            continue
        try:
            thresholds[name] = float(value)
        except (TypeError, ValueError) as err:
            raise ConfigError(f"{name} must be a number") from err

    for low, high in (("warn_above", "critical_above"), ("critical_below", "warn_below")):
        if low in thresholds and high in thresholds and thresholds[low] > thresholds[high]:
            raise ConfigError(f"{low} must not be above {high}")

    return {"muted": bool(data.get("muted", False)), "thresholds": thresholds}


def redact(source: dict[str, Any]) -> dict[str, Any]:
    """Copy a source with secrets replaced by a set/unset marker."""
    safe = dict(source)
    for field in SECRET_FIELDS:
        if field in safe:
            safe[field] = "" if not safe[field] else "__stored__"
    return safe


class Store:
    """JSON-backed source list in the add-on's persistent volume."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = {"sources": [], "api_token": "", "overrides": {}}
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        if not self._path.exists():
            # Assigned, not setdefault: the initial value is an empty string,
            # which setdefault would treat as already present and leave in
            # place, producing an add-on that accepts "Bearer ".
            self._data["api_token"] = secrets.token_urlsafe(32)
            self.save()
            return
        try:
            loaded = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.error("Could not read %s, starting empty: %s", self._path, err)
            loaded = {}
        self._data = {
            "sources": list(loaded.get("sources") or []),
            "api_token": loaded.get("api_token") or secrets.token_urlsafe(32),
            "overrides": dict(loaded.get("overrides") or {}),
        }
        if not loaded.get("api_token"):
            self.save()

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot truncate the config.
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._data, indent=2))
        temp.replace(self._path)

    # -- accessors --------------------------------------------------------

    @property
    def api_token(self) -> str:
        return self._data["api_token"]

    @property
    def sources(self) -> list[dict[str, Any]]:
        return list(self._data["sources"])

    def get(self, source_id: str) -> dict[str, Any] | None:
        return next((s for s in self._data["sources"] if s["id"] == source_id), None)

    def add(self, data: dict[str, Any]) -> dict[str, Any]:
        source = validate_source(data)
        self._data["sources"].append(source)
        self.save()
        return source

    def update(self, source_id: str, data: dict[str, Any]) -> dict[str, Any]:
        existing = self.get(source_id)
        if existing is None:
            raise ConfigError(f"No such source: {source_id}")
        merged = validate_source({**data, "id": source_id}, existing=existing)
        index = self._data["sources"].index(existing)
        self._data["sources"][index] = merged
        self.save()
        return merged

    def remove(self, source_id: str) -> None:
        existing = self.get(source_id)
        if existing is None:
            raise ConfigError(f"No such source: {source_id}")
        self._data["sources"].remove(existing)
        # Otherwise a re-added source would silently inherit the old one's
        # mutes and thresholds.
        self._data["overrides"].pop(source_id, None)
        self.save()

    # -- per-line overrides -----------------------------------------------
    #
    # Deliberately kept out of the source dict: `Manager.sync` rebuilds a
    # runner whenever its config changes, so storing these alongside the
    # credentials would restart the poller — re-authenticating and dropping the
    # current snapshot — every time a threshold was nudged.

    def overrides_for(self, source_id: str) -> dict[str, Any]:
        return dict(self._data["overrides"].get(source_id) or {})

    def set_override(
        self, source_id: str, entity_key: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        if self.get(source_id) is None:
            raise ConfigError(f"No such source: {source_id}")
        override = validate_override(data)
        # A line back at its defaults is stored as nothing, so the file does
        # not accumulate an entry per monitor that was merely looked at.
        if not override["muted"] and not override["thresholds"]:
            self.clear_override(source_id, entity_key)
            return override
        self._data["overrides"].setdefault(source_id, {})[entity_key] = override
        self.save()
        return override

    def clear_override(self, source_id: str, entity_key: str) -> None:
        source = self._data["overrides"].get(source_id)
        if not source or entity_key not in source:
            return
        del source[entity_key]
        if not source:
            del self._data["overrides"][source_id]
        self.save()
