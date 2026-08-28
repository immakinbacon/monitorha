"""Constants for the Infrastructure Monitor integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "monitorha"

# Fired on the Home Assistant bus for every change the add-on detects, so an
# automation can trigger on it:
#
#   triggers:
#     - trigger: event
#       event_type: monitorha_event
#       event_data:
#         kind: threshold_critical
EVENT_MONITOR: Final = "monitorha_event"

CONF_TOKEN: Final = "token"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# The add-on does the real device polling on its own tiered schedule; this is
# only how often Home Assistant reads the result over the local network, so it
# can be frequent and cheap.
DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_PORT: Final = 8099
DEFAULT_TIMEOUT: Final = 30

# Key of a source's primary device. Sub-devices (Proxmox nodes, guests, ...)
# use their own keys and are linked with `via_device`.
MAIN: Final = "main"
