"""Config flow + Options flow for the Eleven Energy Plus integration.

Setup offers two ways in (a menu):

* **Sign in with an Eleven Energy account** (``pick_implementation``): an
  OAuth2 + PKCE flow against Eleven Energy's own sign-in page (see
  :mod:`.oauth`). Home Assistant never handles a credential the user typed,
  the token is scoped to one site and can be withdrawn from the Eleven
  Energy app, and Home Assistant refreshes it automatically.
* **Use an API token** (``token``): paste a token created in the Eleven
  Energy app. Right for headless setups, and what every installation before
  2.0 uses. The token is checked against the API before it is stored,
  telling a rejected token (401/403, ``invalid_auth``) apart from an
  unreachable API (``cannot_connect``).

The same menu is offered by **Reconfigure**, so an existing installation can
move between the two without losing its devices, entities or history, and
**reauthentication** asks for whichever kind of credential the entry uses.
Both replace the entry's data wholesale (so an entry never carries both kinds
of credential) and then schedule a reload themselves; the integration's
update listener deliberately ignores credential changes, so there is never a
second reload racing the first.

The Options flow covers only what is safe to change live: the poll interval
and the device-label override. Only one Eleven Energy Plus entry is
supported (``single_config_entry`` in the manifest).
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    BASE_URL,
    CONF_AUTH_IMPLEMENTATION,
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    CONF_TOKEN,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_STEP_SECONDS,
)
from .oauth import async_register_implementation

_LOGGER = logging.getLogger(__name__)

ENTRY_TITLE = "Eleven Energy Plus"

# Tight timeout: the user is sitting in front of a form waiting for this.
_CONFIG_FLOW_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=10)

_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


async def _check_token(hass: HomeAssistant, token: str) -> None:
    """Ask the API whether it accepts ``token``, and say why when it does not.

    Raises :class:`InvalidAuth` for 401/403 and :class:`CannotConnect` for a
    timeout, a transport failure or any other non-200 status. Anything not
    anticipated propagates, so the flow logs it with its traceback and
    reports ``unknown`` instead of hiding the evidence behind "cannot
    connect".
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
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Eleven Energy Plus token check failed: %r", err)
        raise CannotConnect from err


async def _async_token_errors(hass: HomeAssistant, token: str) -> dict[str, str]:
    """Validate ``token`` and return the form errors (empty when it works)."""
    if not token:
        return {"base": "invalid_auth"}
    try:
        await _check_token(hass, token)
    except InvalidAuth:
        return {"base": "invalid_auth"}
    except CannotConnect:
        return {"base": "cannot_connect"}
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Unexpected exception validating the Eleven Energy token")
        return {"base": "unknown"}
    return {}


def _default_options() -> dict[str, Any]:
    return {CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS}


class ConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle setup, reconfiguration and reauthentication for Eleven Energy Plus."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        """Return the logger, as the OAuth flow handler requires."""
        return _LOGGER

    @callback
    def _async_replace_credentials(
        self, entry: ConfigEntry, data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Replace an existing entry's credential, then reload it.

        ``data`` replaces the entry's data rather than being merged into it,
        so moving between an API token and a sign-in never leaves both
        behind. The reload is scheduled here because the update listener
        ignores credential changes (an account sign-in refreshing its access
        token must not reload anything).
        """
        result = self.async_update_and_abort(entry, data=dict(data))
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the choice between signing in and pasting an API token."""
        async_register_implementation(self.hass)
        return self.async_show_menu(
            step_id="user", menu_options=["pick_implementation", "token"]
        )

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up (or, from Reconfigure, switch to) an API token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN, "")).strip()
            errors = await _async_token_errors(self.hass, token)
            if not errors:
                if self.source == SOURCE_RECONFIGURE:
                    return self._async_replace_credentials(
                        self._get_reconfigure_entry(), {CONF_TOKEN: token}
                    )
                return self.async_create_entry(
                    title=ENTRY_TITLE,
                    data={CONF_TOKEN: token},
                    options=_default_options(),
                )

        return self.async_show_form(
            step_id="token", data_schema=_TOKEN_SCHEMA, errors=errors
        )

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Finish a sign-in: create the entry, or update the existing one.

        From reauthentication or Reconfigure the existing entry keeps its
        identity, devices and entities; only the credential changes. Creating
        a second entry instead would orphan every entity on the first.
        """
        if self.source == SOURCE_REAUTH:
            return self._async_replace_credentials(self._get_reauth_entry(), data)
        if self.source == SOURCE_RECONFIGURE:
            return self._async_replace_credentials(
                self._get_reconfigure_entry(), data
            )
        return self.async_create_entry(
            title=ENTRY_TITLE, data=data, options=_default_options()
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change how an existing installation connects (sign-in or API token)."""
        async_register_implementation(self.hass)
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["pick_implementation", "token"]
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The credential stopped working; ask for the same kind again."""
        if CONF_AUTH_IMPLEMENTATION in entry_data:
            return await self.async_step_reauth_account()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm, then sign in to the Eleven Energy account again."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_account")
        async_register_implementation(self.hass)
        return await self.async_step_pick_implementation()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a replacement API token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN, "")).strip()
            errors = await _async_token_errors(self.hass, token)
            if not errors:
                return self._async_replace_credentials(
                    self._get_reauth_entry(), {CONF_TOKEN: token}
                )

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_TOKEN_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Eleven Energy Plus options: poll interval and device-label override.

    The credential is deliberately not here: it is changed through
    Reconfigure or reauthentication, which handle both kinds of credential
    and cannot corrupt an account sign-in entry. Intentionally has no
    ``__init__`` - ``self.config_entry`` is provided by the base class.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Render and process the options form.

        The interval clamp is defence in depth: the slider already enforces
        the bounds. The device-label override is a free-text string; blank
        means "use the label the API reports", and changing it reloads the
        integration because device info is registered at platform setup.
        """
        if user_input is not None:
            try:
                new_interval = int(user_input[CONF_POLL_INTERVAL])
            except (KeyError, TypeError, ValueError):
                new_interval = DEFAULT_POLL_INTERVAL_SECONDS
            new_interval = max(
                MIN_POLL_INTERVAL_SECONDS,
                min(MAX_POLL_INTERVAL_SECONDS, new_interval),
            )
            device_label = str(user_input.get(CONF_DEVICE_LABEL, "") or "").strip()
            return self.async_create_entry(
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
        )
