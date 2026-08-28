"""Backend clients and the factory that builds one from a source config."""

from __future__ import annotations

import ssl
from typing import Any

import aiohttp

from ..const import (
    AUTH_TOKEN,
    DEFAULT_PORTS,
    POWER_OFF_GRACEFUL,
    TYPE_MIKROTIK,
    TYPE_PROXMOX,
    TYPE_REDFISH,
    TYPE_SWOS,
)
from .base import AuthenticationError, BaseClient, ConnectionFailed, MonitorError
from .mikrotik import MikrotikClient
from .proxmox import SCOPE_NODE, ProxmoxClient
from .redfish import RedfishClient
from .swos import SwosClient

__all__ = [
    "AuthenticationError",
    "BaseClient",
    "ConnectionFailed",
    "MonitorError",
    "MikrotikClient",
    "ProxmoxClient",
    "RedfishClient",
    "SwosClient",
    "build_client",
    "make_session",
]


def make_session(verify_ssl: bool) -> aiohttp.ClientSession:
    """Create a session, optionally tolerating self-signed certificates.

    Self-signed certificates are the norm on BMCs, on Proxmox and on RouterOS,
    so verification is opt-in per source rather than global.
    """
    if verify_ssl:
        connector = aiohttp.TCPConnector()
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=context)
    return aiohttp.ClientSession(connector=connector)


def build_client(
    config: dict[str, Any], session: aiohttp.ClientSession
) -> BaseClient:
    """Construct the backend client described by a source config."""
    source_type = config["type"]
    host = config["host"]
    port = int(config.get("port") or DEFAULT_PORTS[source_type])

    if source_type == TYPE_MIKROTIK:
        return MikrotikClient(
            session,
            host,
            port,
            config.get("username", ""),
            config.get("password", ""),
            use_ssl=bool(config.get("use_ssl", True)),
            monitor_interfaces=bool(config.get("monitor_interfaces", True)),
            monitor_tunnels=bool(config.get("monitor_tunnels", True)),
            monitor_netwatch=bool(config.get("monitor_netwatch", True)),
        )

    if source_type == TYPE_PROXMOX:
        return ProxmoxClient(
            session,
            host,
            port,
            auth_method=config.get("auth_method", AUTH_TOKEN),
            username=config.get("username", ""),
            password=config.get("password", ""),
            token_id=config.get("token_id", ""),
            token_secret=config.get("token_secret", ""),
            monitor_guests=bool(config.get("monitor_guests", True)),
            monitor_backups=bool(config.get("monitor_backups", True)),
            monitor_interfaces=bool(config.get("monitor_interfaces", True)),
            scope=str(config.get("scope") or SCOPE_NODE),
            node=str(config.get("node") or ""),
        )

    if source_type == TYPE_SWOS:
        return SwosClient(
            session,
            host,
            port,
            config.get("username", ""),
            config.get("password", ""),
            use_ssl=bool(config.get("use_ssl", False)),
            monitor_ports=bool(config.get("monitor_ports", True)),
            monitor_poe=bool(config.get("monitor_poe", True)),
            monitor_sfp=bool(config.get("monitor_sfp", True)),
            check_firmware=bool(config.get("check_firmware", True)),
        )

    if source_type == TYPE_REDFISH:
        return RedfishClient(
            session,
            host,
            port,
            config.get("username", ""),
            config.get("password", ""),
            power_off_action=config.get("power_off_action", POWER_OFF_GRACEFUL),
        )

    raise ValueError(f"Unknown source type: {source_type}")
