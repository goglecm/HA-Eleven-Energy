"""Tests for oauth.py: the public PKCE client used for sign-in."""

from __future__ import annotations

import base64
import hashlib

import pytest
from yarl import URL

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    DOMAIN,
    OAUTH_CLIENT_ID,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
)
from custom_components.eleven_energy_plus.oauth import (
    ElevenEnergyOAuth2Implementation,
    async_register_implementation,
)

from .common import site_summary

TOKEN_RESPONSE = {
    "access_token": "access",
    "refresh_token": "refresh",
    "token_type": "Bearer",
    "expires_in": 3600,
}


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.mark.guard("D24")
@pytest.mark.covers("FC-FLOW-03", "FC-FLOW-11")
@pytest.mark.usefixtures("current_request_with_host")
async def test_challenge_matches_the_verifier_sent_at_code_exchange(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{BASE_URL}site", json={"devices": [site_summary()]})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pick_implementation"}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    query = URL(result["url"]).query
    assert query["code_challenge_method"] == "S256"
    state = config_entry_oauth2_flow._decode_jwt(hass, query["state"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"state": state, "code": "auth-code"}
    )
    await hass.config_entries.flow.async_configure(result["flow_id"])

    exchanges = [call for call in aioclient_mock.mock_calls if str(call[1]) == OAUTH_TOKEN_URL]
    assert len(exchanges) == 1
    verifier = exchanges[0][2]["code_verifier"]
    assert 43 <= len(verifier) <= 128, "RFC 7636 verifier length"
    assert _s256(verifier) == query["code_challenge"]


@pytest.mark.covers("FC-FLOW-11")
async def test_each_authorization_takes_a_fresh_verifier(hass: HomeAssistant) -> None:
    implementation = ElevenEnergyOAuth2Implementation(hass)
    first = implementation.extra_authorize_data
    first_verifier = implementation.code_verifier
    second = implementation.extra_authorize_data
    assert implementation.code_verifier != first_verifier
    assert first["code_challenge"] != second["code_challenge"]
    assert second["code_challenge"] == _s256(implementation.code_verifier)
    assert second["scope"] == " ".join(OAUTH_SCOPES)


@pytest.mark.covers("FC-FLOW-03")
async def test_implementation_identity(hass: HomeAssistant) -> None:
    implementation = ElevenEnergyOAuth2Implementation(hass)
    assert implementation.name == "Eleven Energy account"
    assert implementation.client_id == OAUTH_CLIENT_ID
    assert implementation.redirect_uri == config_entry_oauth2_flow.MY_AUTH_CALLBACK_PATH


async def test_registration_is_idempotent(hass: HomeAssistant) -> None:
    async_register_implementation(hass)
    async_register_implementation(hass)
    implementations = await config_entry_oauth2_flow.async_get_implementations(hass, DOMAIN)
    assert list(implementations) == [DOMAIN]
