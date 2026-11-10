"""Signing in with an Eleven Energy account (OAuth2 with PKCE).

Home Assistant's usual arrangement asks the user to create their own OAuth
client and paste its id and secret in. That is the wrong answer here:
everybody installing this integration would be registering a client for a
service they do not run, to obtain a secret that would then sit in their
config directory.

Instead the integration uses Eleven Energy's public client id for Home
Assistant, with no secret - which is honest about what a published
application can keep - and the authorization server requires PKCE from it,
so an intercepted authorization code is useless to anyone who did not start
the flow.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_entry_oauth2_flow

from .const import (
    DOMAIN,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
)


class ElevenEnergyOAuth2Implementation(
    config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce
):
    """The Eleven Energy public OAuth client."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the implementation."""
        super().__init__(
            hass,
            DOMAIN,
            OAUTH_CLIENT_ID,
            OAUTH_AUTHORIZE_URL,
            OAUTH_TOKEN_URL,
        )

    @property
    def name(self) -> str:
        """What the user is choosing, if they are ever shown a choice."""
        return "Eleven Energy account"

    @property
    def redirect_uri(self) -> str:
        """Always use the my.home-assistant.io redirect.

        It is the only redirect Eleven Energy registered for this client, so
        the instance's own address (which Home Assistant falls back to when
        the ``my`` integration is not loaded) could never be accepted.
        Returning it here, rather than declaring ``my`` as a dependency,
        keeps the integration from forcing the frontend stack to load.
        """
        return config_entry_oauth2_flow.MY_AUTH_CALLBACK_PATH

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Add the scopes, and take a fresh PKCE verifier for each authorization.

        The base class generates one verifier when the implementation is
        created and the implementation lives as long as Home Assistant does,
        so every authorization would otherwise share it. This property is read
        once per authorize URL, so regenerating here scopes the verifier to
        the attempt that uses it. Two simultaneous sign-ins on one instance
        would make the first fail at the code exchange and need retrying,
        which is a better failure than a verifier shared for the life of the
        process.
        """
        self.code_verifier = self.generate_code_verifier()
        data = {"scope": " ".join(OAUTH_SCOPES)}
        data.update(super().extra_authorize_data)
        return data


@callback
def async_register_implementation(hass: HomeAssistant) -> None:
    """Make the sign-in implementation available to config flows and setup.

    Called from ``async_setup`` and from the config flow itself: setting up
    the integration only happens when there is a config entry to load, so on
    a first install the flow would otherwise find nothing registered and
    abort with ``missing_configuration``. Registration is keyed by domain and
    replaces what was there, so calling it more than once is harmless;
    in-progress flows keep the instance (and verifier) they started with.
    """
    config_entry_oauth2_flow.async_register_implementation(
        hass, DOMAIN, ElevenEnergyOAuth2Implementation(hass)
    )
