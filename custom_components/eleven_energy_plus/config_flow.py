"""Config flow + Options flow for the Eleven Energy Plus integration.

Setup offers two ways in (a menu):

* **Sign in with an Eleven Energy account** (``pick_implementation``): an
  OAuth2 + PKCE flow against Eleven Energy's own sign-in page (see
  :mod:`.oauth`). Home Assistant never handles a credential the user typed,
  the token is scoped to one site and can be withdrawn from the Eleven
  Energy app, and Home Assistant refreshes it automatically.
* **Use an API token** (``token``): paste a token created in the Eleven
  Energy app. Right for headless setups, and what every installation before
  2.0 uses.

Whichever way the credential arrives - setup, reauthentication or
Reconfigure, token or sign-in - it is checked against the API before it is
stored, and refused when:

* the API rejects it (``invalid_auth``) or cannot be reached
  (``cannot_connect``);
* its site lists no hybrid inverter (``no_inverters``), which would leave an
  entry retrying for ever;
* it belongs to a different site than the entry being reauthenticated or
  reconfigured (``different_site``), which would orphan every device.

A token is refused with a form error, so another can be pasted; a sign-in
ends the flow with the same reason.

The same menu is offered by **Reconfigure**, so an existing installation can
move between the two without losing its devices, entities or history, and
**reauthentication** asks for whichever kind of credential the entry uses.
Both replace the entry's data wholesale (so an entry never carries both kinds
of credential) and then schedule a reload themselves; the integration's
update listener deliberately ignores credential changes, so there is never a
second reload racing the first.

The Options flow covers only what is safe to change live: the poll interval
(a whole number of seconds) and the device-label override (blank, or up to
:data:`~.const.MAX_DEVICE_LABEL_LENGTH` printable characters). Only one
Eleven Energy Plus entry is supported (``single_config_entry`` in the
manifest).

Schemas are built with ``voluptuous``, which Home Assistant 2026.9+ aliases
to ``probatio`` at runtime; to a type checker they are different types, so
the schemas handed to Home Assistant are annotated ``Any``.
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
from homeassistant.helpers import config_entry_oauth2_flow, device_registry as dr
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
    MAX_DEVICE_LABEL_LENGTH,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_STEP_SECONDS,
)
from .controller import listed_inverter_ids
from .oauth import async_register_implementation
from .parsing import parse_whole_number

_LOGGER = logging.getLogger(__name__)

ENTRY_TITLE = "Eleven Energy Plus"

# Tight timeout: the user is sitting in front of a form waiting for this.
_CONFIG_FLOW_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=10)

_TOKEN_SCHEMA: Any = vol.Schema(
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


async def _site_inverters(hass: HomeAssistant, token: str) -> list[str]:
    """Return the hybrid inverters the site of ``token`` lists.

    Raises :class:`InvalidAuth` for 401/403 and :class:`CannotConnect` for a
    timeout, a transport failure, any other non-200 status or a payload that
    is not a site listing. Anything not anticipated propagates, so the flow
    logs it with its traceback and reports ``unknown`` instead of hiding the
    evidence behind "cannot connect".
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
            _LOGGER.debug("Credential check %s -> %s", resp.url, resp.status)
            if resp.status in (401, 403):
                raise InvalidAuth
            if resp.status != 200:
                raise CannotConnect
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Eleven Energy Plus credential check failed: %r", err)
        raise CannotConnect from err
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        _LOGGER.debug("Eleven Energy Plus credential check got %r", payload)
        raise CannotConnect
    return listed_inverter_ids(payload)


def _known_inverters(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Return the inverter ids ``entry`` has devices for."""
    return {
        identifier
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN
    }


async def _async_credential_error(
    hass: HomeAssistant, token: str, entry: ConfigEntry | None
) -> str | None:
    """Check a credential; return why it cannot be used, or None.

    ``entry`` is the entry being reauthenticated or reconfigured: the new
    credential must reach the same site, recognised by sharing at least one
    of the entry's inverters. An entry with no devices yet cannot be
    compared, so any site is accepted for it.
    """
    if not token:
        return "invalid_auth"
    try:
        inverters = await _site_inverters(hass, token)
    except InvalidAuth:
        return "invalid_auth"
    except CannotConnect:
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected exception validating the Eleven Energy credential")
        return "unknown"
    if not inverters:
        return "no_inverters"
    if entry is not None:
        known = _known_inverters(hass, entry)
        if known and known.isdisjoint(inverters):
            return "different_site"
    return None


def _default_options() -> dict[str, Any]:
    return {CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS}


def _valid_label(label: str) -> bool:
    """Return True for a blank or printable label of acceptable length."""
    return len(label) <= MAX_DEVICE_LABEL_LENGTH and label.isprintable()


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

    def _existing_entry(self) -> ConfigEntry | None:
        """Return the entry being reauthenticated or reconfigured, if any."""
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return None

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

    def _async_store(self, data: Mapping[str, Any]) -> ConfigFlowResult:
        """Store a checked credential: update the entry, or create one."""
        entry = self._existing_entry()
        if entry is not None:
            return self._async_replace_credentials(entry, data)
        return self.async_create_entry(
            title=ENTRY_TITLE, data=dict(data), options=_default_options()
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the choice between signing in and pasting an API token."""
        async_register_implementation(self.hass)
        return self.async_show_menu(
            step_id="user", menu_options=["pick_implementation", "token"]
        )

    async def _async_token_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Take an API token, check it, and store it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN, "")).strip()
            error = await _async_credential_error(self.hass, token, self._existing_entry())
            if error is None:
                return self._async_store({CONF_TOKEN: token})
            errors["base"] = error

        return self.async_show_form(
            step_id=step_id, data_schema=_TOKEN_SCHEMA, errors=errors
        )

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up (or, from Reconfigure, switch to) an API token."""
        return await self._async_token_step("token", user_input)

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Finish a sign-in: check it, then create the entry or update the existing one.

        From reauthentication or Reconfigure the existing entry keeps its
        identity, devices and entities; only the credential changes. Creating
        a second entry instead would orphan every entity on the first.
        """
        token = data.get(CONF_TOKEN)
        access_token = token.get("access_token") if isinstance(token, dict) else None
        error = await _async_credential_error(
            self.hass, access_token if isinstance(access_token, str) else "", self._existing_entry()
        )
        if error is not None:
            return self.async_abort(reason=error)
        return self._async_store(data)

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
        return await self._async_token_step("reauth_confirm", user_input)

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

        The slider keeps the interval in range but lets NaN and fractions
        through, so the value is checked here and refused rather than
        replaced or truncated. The device-label override is blank ("use the
        label the API reports") or printable text of bounded length; changing
        it reloads the integration because device info is registered at
        platform setup.
        """
        errors: dict[str, str] = {}
        current_interval = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS
        )
        current_label = self.config_entry.options.get(CONF_DEVICE_LABEL, "")

        if user_input is not None:
            interval = parse_whole_number(user_input.get(CONF_POLL_INTERVAL))
            if interval is None or not (
                MIN_POLL_INTERVAL_SECONDS <= interval <= MAX_POLL_INTERVAL_SECONDS
            ):
                errors[CONF_POLL_INTERVAL] = "poll_interval_invalid"
            device_label = str(user_input.get(CONF_DEVICE_LABEL, "") or "").strip()
            if not _valid_label(device_label):
                errors[CONF_DEVICE_LABEL] = "label_invalid"
            if not errors:
                return self.async_create_entry(
                    data={
                        **self.config_entry.options,
                        CONF_POLL_INTERVAL: interval,
                        CONF_DEVICE_LABEL: device_label,
                    },
                )
            current_label = device_label

        schema: Any = vol.Schema(
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
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
