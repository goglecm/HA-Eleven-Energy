"""Tests for the Eleven Energy Plus config, reconfigure, reauth and options flows."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
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
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
)

TOKEN_RESPONSE = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "expires_in": 28800,
    "token_type": "Bearer",
}


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep flow tests from setting the created entry up against the API."""
    with patch(
        "custom_components.eleven_energy_plus.async_setup_entry", return_value=True
    ) as mock:
        yield mock


@pytest.fixture
def mock_reload(hass: HomeAssistant) -> Generator[Any]:
    """Record reloads scheduled by the flows without running them."""
    with patch.object(hass.config_entries, "async_schedule_reload") as mock:
        yield mock


def _oauth_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Eleven Energy Plus",
        data={
            "auth_implementation": DOMAIN,
            "token": {**TOKEN_RESPONSE, "expires_at": 9999999999},
        },
        options={CONF_POLL_INTERVAL: 30},
    )


def _token_entry(token: str = "old-token") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Eleven Energy Plus",
        data={"token": token},
        options={CONF_POLL_INTERVAL: 30},
    )


async def _start_user_flow(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _complete_sign_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, result: dict[str, Any]
) -> dict[str, Any]:
    """Drive a flow from the menu through the external sign-in step."""
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pick_implementation"}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    url = URL(result["url"])
    assert str(url.with_query(None)) == OAUTH_AUTHORIZE_URL
    query = url.query
    assert query["client_id"] == OAUTH_CLIENT_ID
    assert query["redirect_uri"] == config_entry_oauth2_flow.MY_AUTH_CALLBACK_PATH
    assert query["scope"] == " ".join(OAUTH_SCOPES)
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"]

    state = config_entry_oauth2_flow._decode_jwt(hass, query["state"])
    assert state is not None
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN_RESPONSE)

    # What Home Assistant's /auth/external/callback view does with the redirect.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"state": state, "code": "auth-code"}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
    result = await hass.config_entries.flow.async_configure(result["flow_id"])

    token_call = aioclient_mock.mock_calls[-1]
    assert str(token_call[1]) == OAUTH_TOKEN_URL
    form = token_call[2]
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == OAUTH_CLIENT_ID
    assert form["code"] == "auth-code"
    assert form["code_verifier"], "PKCE verifier must be sent"
    return result


class TestUserFlow:
    """Initial setup: a menu offering sign-in or an API token."""

    async def test_menu_shown_initially(self, hass: HomeAssistant) -> None:
        result = await _start_user_flow(hass)
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "user"
        assert result["menu_options"] == ["pick_implementation", "token"]

    async def test_token_happy_path_creates_entry(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        mock_setup_entry: AsyncMock,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=200, json={"devices": []})
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "token"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "token"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "  valid-token  "}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Eleven Energy Plus"
        assert result["data"] == {"token": "valid-token"}
        assert result["options"] == {CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS}
        assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer valid-token"

    @pytest.mark.parametrize(
        ("status", "expected_error"),
        [
            (401, "invalid_auth"),
            (403, "invalid_auth"),
            (500, "cannot_connect"),
            (503, "cannot_connect"),
        ],
    )
    async def test_token_error_paths(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        status: int,
        expected_error: str,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=status)
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "token"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "any"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected_error}

    @pytest.mark.parametrize("exc", [aiohttp.ClientError("down"), TimeoutError()])
    async def test_token_transport_errors(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, exc: Exception
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", exc=exc)
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "token"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "any"}
        )
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_token_unexpected_error(self, hass: HomeAssistant) -> None:
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "token"}
        )
        with patch(
            "custom_components.eleven_energy_plus.config_flow._check_token",
            side_effect=ValueError("bug"),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"token": "any"}
            )
        assert result["errors"] == {"base": "unknown"}

    async def test_blank_token_is_refused_without_calling_the_api(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "token"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "   "}
        )
        assert result["errors"] == {"base": "invalid_auth"}
        assert aioclient_mock.call_count == 0

    async def test_sign_in_creates_entry(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        current_request_with_host: None,
        mock_setup_entry: AsyncMock,
    ) -> None:
        result = await _complete_sign_in(hass, aioclient_mock, await _start_user_flow(hass))
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Eleven Energy Plus"
        assert result["data"]["auth_implementation"] == DOMAIN
        assert result["data"]["token"]["access_token"] == "access-1"
        assert result["data"]["token"]["refresh_token"] == "refresh-1"
        assert result["options"] == {CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS}

    async def test_each_authorization_gets_a_fresh_verifier(
        self, hass: HomeAssistant, current_request_with_host: None
    ) -> None:
        challenges = set()
        for _ in range(2):
            result = await _start_user_flow(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"next_step_id": "pick_implementation"}
            )
            challenges.add(URL(result["url"]).query["code_challenge"])
            hass.config_entries.flow.async_abort(result["flow_id"])
        assert len(challenges) == 2

    async def test_only_one_entry_allowed(self, hass: HomeAssistant) -> None:
        _token_entry().add_to_hass(hass)
        result = await _start_user_flow(hass)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "single_instance_allowed"


class TestReconfigure:
    """Reconfigure moves an entry between credential kinds, keeping its identity."""

    async def test_switch_to_api_token(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        mock_reload: Any,
    ) -> None:
        entry = _oauth_entry()
        entry.add_to_hass(hass)
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": []})

        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "token"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "new-token"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        # Replaced, not merged: no sign-in leftovers.
        assert entry.data == {"token": "new-token"}
        assert entry.options == {CONF_POLL_INTERVAL: 30}
        mock_reload.assert_called_once_with(entry.entry_id)
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    async def test_switch_to_sign_in(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        current_request_with_host: None,
        mock_reload: Any,
    ) -> None:
        entry = _token_entry()
        entry.add_to_hass(hass)
        result = await entry.start_reconfigure_flow(hass)
        result = await _complete_sign_in(hass, aioclient_mock, result)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert set(entry.data) == {"auth_implementation", "token"}
        assert entry.data["token"]["access_token"] == "access-1"
        mock_reload.assert_called_once_with(entry.entry_id)

    async def test_invalid_token_keeps_existing_credential(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        mock_reload: Any,
    ) -> None:
        entry = _token_entry()
        entry.add_to_hass(hass)
        aioclient_mock.get(f"{BASE_URL}site", status=401)
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "token"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "bad"}
        )
        assert result["errors"] == {"base": "invalid_auth"}
        assert entry.data == {"token": "old-token"}
        mock_reload.assert_not_called()


class TestReauth:
    """Reauthentication asks for the same kind of credential the entry holds."""

    async def test_api_token_reauth(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        mock_reload: Any,
    ) -> None:
        entry = _token_entry()
        entry.add_to_hass(hass)
        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        aioclient_mock.get(f"{BASE_URL}site", status=401)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "still-bad"}
        )
        assert result["errors"] == {"base": "invalid_auth"}

        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": []})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "fresh-token"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data == {"token": "fresh-token"}
        mock_reload.assert_called_once_with(entry.entry_id)

    async def test_sign_in_reauth(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        current_request_with_host: None,
        mock_reload: Any,
    ) -> None:
        entry = _oauth_entry()
        entry.add_to_hass(hass)
        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_account"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.EXTERNAL_STEP
        state = config_entry_oauth2_flow._decode_jwt(
            hass, URL(result["url"]).query["state"]
        )
        aioclient_mock.post(OAUTH_TOKEN_URL, json={**TOKEN_RESPONSE, "access_token": "access-2"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"state": state, "code": "code-2"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data["token"]["access_token"] == "access-2"
        mock_reload.assert_called_once_with(entry.entry_id)
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1


class TestOptionsFlow:
    """Options cover the poll interval and the device-label override only."""

    async def test_options_flow_shows_current_values(self, hass: HomeAssistant) -> None:
        entry = _token_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"
        fields = {key.schema for key in result["data_schema"].schema}
        assert fields == {CONF_POLL_INTERVAL, CONF_DEVICE_LABEL}, (
            "the credential must not be editable (or shown) in Options"
        )

    async def test_options_flow_persists_changes(self, hass: HomeAssistant) -> None:
        entry = _token_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 60}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {CONF_POLL_INTERVAL: 60, CONF_DEVICE_LABEL: ""}
        assert entry.data == {"token": "old-token"}

    async def test_options_flow_persists_device_label_override(
        self, hass: HomeAssistant
    ) -> None:
        entry = _token_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_POLL_INTERVAL: 30, CONF_DEVICE_LABEL: "  Mediterranean Sea 12  "},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_DEVICE_LABEL] == "Mediterranean Sea 12"

    async def test_options_flow_form_prefills_override(self, hass: HomeAssistant) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"token": "t"},
            options={CONF_POLL_INTERVAL: 30, CONF_DEVICE_LABEL: "Custom Label"},
        )
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        schema_defaults = {
            key.schema: key.default()
            for key in result["data_schema"].schema
            if hasattr(key, "default") and callable(key.default)
        }
        assert schema_defaults[CONF_DEVICE_LABEL] == "Custom Label"
        assert schema_defaults[CONF_POLL_INTERVAL] == 30

    async def test_options_flow_on_sign_in_entry_keeps_credential(
        self, hass: HomeAssistant
    ) -> None:
        """Regression (upstream): Options used to corrupt signed-in entries."""
        entry = _oauth_entry()
        entry.add_to_hass(hass)
        data_before = dict(entry.data)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 45}
        )
        assert entry.data == data_before
