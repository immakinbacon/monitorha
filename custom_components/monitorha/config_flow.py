"""Config flow.

The add-on publishes a Supervisor discovery record, so in the normal case the
user only has to confirm. Manual entry exists because older Supervisor builds
validated the discovery `service` name against a fixed list and will drop a
custom one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import (
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import AddonAuthError, AddonClient, AddonConnectionError

_LOGGER = logging.getLogger(__name__)


class MonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect Home Assistant to the Infrastructure Monitor add-on."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    async def _async_probe(self, data: dict[str, Any]) -> str | None:
        """Return an error key, or None if the add-on answered."""
        client = AddonClient(
            async_get_clientsession(self.hass),
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_TOKEN],
        )
        try:
            await client.async_snapshot()
        except AddonAuthError:
            return "invalid_auth"
        except AddonConnectionError as err:
            _LOGGER.debug("Add-on probe failed: %s", err)
            return "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error probing the add-on")
            return "unknown"
        return None

    # -- manual -----------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # One add-on serves every monitored device, so one entry is enough.
        self._async_abort_entries_match()
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {**user_input, CONF_PORT: int(user_input[CONF_PORT])}
            error = await self._async_probe(data)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Infrastructure Monitor", data=data
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default=user_input.get(CONF_HOST, "") if user_input else ""
                    ): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(CONF_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # -- Supervisor discovery ---------------------------------------------

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle the add-on announcing itself through the Supervisor."""
        config = discovery_info.config
        self._discovered = {
            CONF_HOST: config["host"],
            CONF_PORT: int(config.get("port", DEFAULT_PORT)),
            CONF_TOKEN: config["token"],
        }
        await self.async_set_unique_id(DOMAIN)
        # A reinstalled add-on gets a fresh token, so keep the entry current.
        self._abort_if_unique_id_configured(updates=self._discovered)
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_probe(self._discovered)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title="Infrastructure Monitor", data=self._discovered
                )

        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"host": self._discovered.get(CONF_HOST, "")},
            errors=errors,
        )

    # -- reauth -----------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._discovered = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self._discovered, **user_input}
            error = await self._async_probe(data)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> MonitorOptionsFlow:
        return MonitorOptionsFlow()


class MonitorOptionsFlow(OptionsFlow):
    """How often Home Assistant reads the add-on's snapshot."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=600,
                            step=5,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )
