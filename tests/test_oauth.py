"""Tests for oauth.py: the public PKCE client used for sign-in."""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from unittest.mock import patch

import pytest
from yarl import URL

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry
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
from custom_components.eleven_energy_plus import oauth
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
@pytest.mark.usefixtures("current_request_with_host")
async def test_each_authorization_takes_a_fresh_verifier(hass: HomeAssistant) -> None:
    implementation = ElevenEnergyOAuth2Implementation(hass)
    first = URL(await implementation.async_generate_authorize_url("flow")).query
    first_verifier = implementation.code_verifier
    second = URL(await implementation.async_generate_authorize_url("flow")).query
    assert implementation.code_verifier != first_verifier
    assert first["code_challenge"] != second["code_challenge"]
    assert second["code_challenge"] == _s256(implementation.code_verifier)
    assert second["scope"] == " ".join(OAUTH_SCOPES)


async def _authorize(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Move a flow to the browser step; return its PKCE challenge and state."""
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    query = URL(result["url"]).query
    return query["code_challenge"], config_entry_oauth2_flow._decode_jwt(hass, query["state"])


async def _exchanged_verifier(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    flow_id: str,
    state: dict[str, Any],
) -> str:
    """Return from the browser and report the verifier sent at code exchange."""
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"state": state, "code": "auth-code"}
    )
    await hass.config_entries.flow.async_configure(result["flow_id"])
    exchanges = [call for call in aioclient_mock.mock_calls if str(call[1]) == OAUTH_TOKEN_URL]
    verifier: str = exchanges[-1][2]["code_verifier"]
    return verifier


@pytest.mark.defect("D35")
@pytest.mark.covers("FC-FLOW-11")
@pytest.mark.usefixtures("current_request_with_host")
async def test_two_sign_ins_open_at_once_each_keep_their_own_verifier(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{BASE_URL}site", json={"devices": [site_summary()]})
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    pick = {"next_step_id": "pick_implementation"}
    challenge, state = await _authorize(hass, first["flow_id"], pick)
    await _authorize(hass, second["flow_id"], pick)

    verifier = await _exchanged_verifier(hass, aioclient_mock, first["flow_id"], state)
    assert _s256(verifier) == challenge, (
        "the second sign-in must not replace the first one's verifier"
    )


@pytest.mark.defect("D35")
@pytest.mark.covers("FC-FLOW-08")
@pytest.mark.usefixtures("current_request_with_host")
async def test_a_reauthentication_survives_a_reconfigure_started_alongside(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_implementation": DOMAIN,
            "token": {**TOKEN_RESPONSE, "expires_at": 9999999999},
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{BASE_URL}site", json={"devices": [site_summary()]})
    reconfigure = await entry.start_reconfigure_flow(hass)
    reauth = await entry.start_reauth_flow(hass)
    challenge, state = await _authorize(hass, reauth["flow_id"], {})
    await _authorize(hass, reconfigure["flow_id"], {"next_step_id": "pick_implementation"})

    with patch.object(hass.config_entries, "async_schedule_reload"):
        verifier = await _exchanged_verifier(hass, aioclient_mock, reauth["flow_id"], state)
    assert _s256(verifier) == challenge


@pytest.mark.covers("FC-FLOW-11")
@pytest.mark.usefixtures("current_request_with_host")
async def test_an_unknown_sign_in_is_never_sent_another_flows_verifier(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN_RESPONSE)
    implementation = ElevenEnergyOAuth2Implementation(hass)
    await implementation.async_generate_authorize_url("pending")
    pending = implementation.code_verifier
    state = {"flow_id": "unknown", "redirect_uri": implementation.redirect_uri}
    await implementation.async_resolve_external_data({"code": "c", "state": state})
    assert aioclient_mock.mock_calls[-1][2]["code_verifier"] != pending


@pytest.mark.covers("FC-FLOW-11")
@pytest.mark.usefixtures("current_request_with_host")
async def test_abandoned_sign_ins_are_not_remembered_without_limit(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN_RESPONSE)
    implementation = ElevenEnergyOAuth2Implementation(hass)
    await implementation.async_generate_authorize_url("oldest")
    oldest = implementation.code_verifier
    for number in range(oauth.MAX_PENDING_SIGN_INS):
        await implementation.async_generate_authorize_url(f"flow-{number}")
    newest = implementation.code_verifier
    assert len(implementation._verifiers) == oauth.MAX_PENDING_SIGN_INS

    redirect = implementation.redirect_uri
    await implementation.async_resolve_external_data(
        {"code": "c", "state": {"flow_id": "oldest", "redirect_uri": redirect}}
    )
    assert aioclient_mock.mock_calls[-1][2]["code_verifier"] != oldest
    await implementation.async_resolve_external_data(
        {"code": "c", "state": {"flow_id": f"flow-{oauth.MAX_PENDING_SIGN_INS - 1}", "redirect_uri": redirect}}
    )
    assert aioclient_mock.mock_calls[-1][2]["code_verifier"] == newest
    assert len(implementation._verifiers) == oauth.MAX_PENDING_SIGN_INS - 1, "used once, then dropped"


@pytest.mark.covers("FC-FLOW-11")
@pytest.mark.usefixtures("current_request_with_host")
async def test_authorizing_the_same_flow_again_replaces_its_verifier(
    hass: HomeAssistant,
) -> None:
    implementation = ElevenEnergyOAuth2Implementation(hass)
    await implementation.async_generate_authorize_url("first")
    await implementation.async_generate_authorize_url("again")
    await implementation.async_generate_authorize_url("first")
    assert list(implementation._verifiers) == ["again", "first"]
    assert implementation._verifiers["first"] == implementation.code_verifier


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
