"""Config flow + Options flow for the Eleven Energy Plus integration.

Two related flows live here:

* :class:`ConfigFlow` - the initial UI-driven setup. The user supplies an API
  token; we hit the site endpoint once to verify it, distinguishing 401/403
  (``invalid_auth``) from any other failure (``cannot_connect``), and bail
  out if an Eleven Energy Plus entry already exists so users can't
  accidentally register the same site twice.
* :class:`OptionsFlowHandler` - the "Configure" button on the integration
  page. Lets the user update the token and the poll interval. The token is
  validated against the API again before being persisted, and the integer
  interval is clamped server-side as a belt-and-braces fallback even though
  the slider already enforces the bounds.

Both flows share :func:`_check_token`, which uses a tight 15-second timeout
so the UI doesn't appear hung while we wait on the upstream API.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    BASE_URL,
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_STEP_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Tight timeout: the user is sitting in front of a form waiting for this.
_CONFIG_FLOW_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=10)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("token", description="API Token"): str,
    }
)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


async def _check_token(hass: HomeAssistant, token: str) -> None:
    """Verify the API token by making a single site request.

    Raises :class:`InvalidAuth` for 401/403 responses and :class:`CannotConnect`
    for any other failure (timeout, DNS, 5xx, etc.) so the user sees the right
    error string in the config flow instead of a generic "cannot connect".
    """
    session = async_get_clientsession(hass)
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with session.get(
            f"{BASE_URL}site", headers=headers, timeout=_CONFIG_FLOW_TIMEOUT
        ) as resp:
            _LOGGER.debug("Token check %s -> %s", resp.url, resp.status)
            if resp.status in (401, 403):
                raise InvalidAuth
            if resp.status != 200:
                raise CannotConnect
    except (InvalidAuth, CannotConnect):
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Eleven Energy Plus token check raised %s", err)
        raise CannotConnect from err


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user-supplied token by making a probe API call.

    Returns the dict the caller passes to :meth:`ConfigFlow.async_create_entry`
    when validation succeeds. Raises :class:`InvalidAuth` for 401/403 and
    :class:`CannotConnect` for any other failure.
    """
    await _check_token(hass, data["token"])
    return {"title": "Eleven Energy Plus"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow for Eleven Energy Plus.

    Only a single Eleven Energy Plus site is supported per Home Assistant
    instance; a second attempt is aborted with the ``already_setup`` reason.
    On success the new entry is created with the default poll interval in
    ``options`` so the Number entity and OptionsFlow have something sensible
    to read on the first start.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Render and process the token form.

        The flow is single-step: validate the token, and on success create the
        entry. Errors are surfaced via the ``errors`` dict so the form is
        re-rendered with an inline message.
        """

        entries = self.hass.config_entries.async_entries(DOMAIN)
        if entries:
            return self.async_abort(reason="already_setup")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=info["title"],
                    data=user_input,
                    options={CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS},
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Eleven Energy Plus Options Flow handler.

    Intentionally has no ``__init__`` - ``self.config_entry`` is provided by
    the :class:`OptionsFlow` base class. Manually assigning it is deprecated
    and stops working in Home Assistant 2025.12.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Render and process the options form.

        Validates the (possibly new) token against the API, persists it on
        the entry's ``data`` (so :func:`_async_options_updated` notices the
        change and triggers a reload), and saves the clamped poll interval
        plus the optional device-label override on the entry's ``options``.
        The clamp here is defence-in-depth: the :class:`NumberSelector`
        schema already enforces the interval bounds, but a manually-crafted
        UI payload could otherwise slip past.

        The device-label override is intentionally a free-text string. An
        empty string means "no override - keep using the API-derived label".
        Changing it triggers a full reload from :func:`_async_options_updated`
        because device-card info is captured at platform-setup time and
        applying a new label to an already-registered device cleanly
        requires re-running entity registration.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input["token"]
            try:
                await validate_input(self.hass, {"token": token})
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                new_interval = int(user_input[CONF_POLL_INTERVAL])
                new_interval = max(
                    MIN_POLL_INTERVAL_SECONDS,
                    min(MAX_POLL_INTERVAL_SECONDS, new_interval),
                )
                # Normalise the override: collapse whitespace, treat blanks
                # as "unset" so the option stays comparable across save/load
                # cycles and ``_async_options_updated`` can detect changes
                # without false positives on leading/trailing spaces.
                device_label = str(user_input.get(CONF_DEVICE_LABEL, "")).strip()
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={"token": token},
                    title="Eleven Energy Plus",
                )
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_POLL_INTERVAL: new_interval,
                        CONF_DEVICE_LABEL: device_label,
                    },
                )

        current_interval = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS
        )
        current_label = self.config_entry.options.get(CONF_DEVICE_LABEL, "")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "token", default=self.config_entry.data["token"]
                    ): str,
                    vol.Required(
                        CONF_POLL_INTERVAL, default=current_interval
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL_SECONDS,
                            max=MAX_POLL_INTERVAL_SECONDS,
                            step=POLL_INTERVAL_STEP_SECONDS,
                            mode=NumberSelectorMode.SLIDER,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Optional(
                        CONF_DEVICE_LABEL, default=current_label
                    ): TextSelector(TextSelectorConfig()),
                }
            ),
            errors=errors,
        )
