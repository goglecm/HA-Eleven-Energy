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

# Sign-ins waiting for the browser to come back, beyond which the oldest is
# forgotten (it then fails at the code exchange and can be started again).
MAX_PENDING_SIGN_INS = 16


class ElevenEnergyOAuth2Implementation(
    config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce
):
    """The Eleven Energy public OAuth client, with a PKCE verifier per flow.

    The base class keeps one ``code_verifier`` on the implementation, which
    is shared by every flow that picks it: two sign-ins open at once (say a
    reconfigure and a reauthentication) would overwrite each other's
    verifier, and the first to finish would fail at the code exchange. Here
    each flow gets its own verifier when its authorize URL is generated,
    and the exchange uses the verifier of the flow named in the returned
    ``state``. Both base methods read ``code_verifier`` without awaiting in
    between, so setting it just before calling them cannot be interleaved.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the implementation."""
        super().__init__(
            hass,
            DOMAIN,
            OAUTH_CLIENT_ID,
            OAUTH_AUTHORIZE_URL,
            OAUTH_TOKEN_URL,
        )
        # Flow id -> verifier, oldest first.
        self._verifiers: dict[str, str] = {}

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
        """Add the scopes to the PKCE challenge the base class sends."""
        data = {"scope": " ".join(OAUTH_SCOPES)}
        data.update(super().extra_authorize_data)
        return data

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """Take a fresh verifier for this flow and build its authorize URL."""
        verifier = self.generate_code_verifier()
        self._verifiers.pop(flow_id, None)
        self._verifiers[flow_id] = verifier
        while len(self._verifiers) > MAX_PENDING_SIGN_INS:
            del self._verifiers[next(iter(self._verifiers))]
        self.code_verifier = verifier
        return await super().async_generate_authorize_url(flow_id)

    async def async_resolve_external_data(self, external_data: Any) -> dict[str, Any]:
        """Exchange the code using the verifier of the flow it was issued to.

        A flow this instance does not know (forgotten, or started before a
        restart) gets a verifier of its own that the server will refuse,
        never another flow's.
        """
        verifier: str | None = self._verifiers.pop(external_data["state"]["flow_id"], None)
        self.code_verifier = verifier or self.generate_code_verifier()
        return await super().async_resolve_external_data(external_data)


@callback
def async_register_implementation(hass: HomeAssistant) -> None:
    """Make the sign-in implementation available to config flows and setup.

    Called from ``async_setup`` and from the config flow itself: setting up
    the integration only happens when there is a config entry to load, so on
    a first install the flow would otherwise find nothing registered and
    abort with ``missing_configuration``. Registration is keyed by domain and
    replaces what was there, so calling it more than once is harmless;
    in-progress flows keep the instance they picked.
    """
    config_entry_oauth2_flow.async_register_implementation(
        hass, DOMAIN, ElevenEnergyOAuth2Implementation(hass)
    )
