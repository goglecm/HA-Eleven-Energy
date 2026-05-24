"""Tests for the Eleven Energy config flow and options flow."""

from __future__ import annotations

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
)


async def _initial_form(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


class TestConfigFlow:
    """User-initiated setup flow."""

    async def test_form_shown_initially(self, hass: HomeAssistant) -> None:
        result = await _initial_form(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

    async def test_happy_path_creates_entry(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=200, json={"devices": []})

        result = await _initial_form(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "valid-token"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Eleven Energy Plus"
        assert result["data"] == {"token": "valid-token"}
        assert result["options"] == {
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS
        }

    @pytest.mark.parametrize(
        ("status", "expected_error"),
        [
            (401, "invalid_auth"),
            (403, "invalid_auth"),
            (500, "cannot_connect"),
            (503, "cannot_connect"),
        ],
    )
    async def test_error_paths(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        status: int,
        expected_error: str,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=status)

        result = await _initial_form(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "any"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected_error}

    async def test_only_one_entry_allowed(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        # An entry already exists.
        MockConfigEntry(domain=DOMAIN, data={"token": "x"}).add_to_hass(hass)

        result = await _initial_form(hass)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_setup"


class TestOptionsFlow:
    """Options flow lets the user update token and poll interval."""

    async def test_options_flow_shows_current_values(
        self, hass: HomeAssistant
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"token": "old-token"},
            options={CONF_POLL_INTERVAL: 30},
        )
        entry.add_to_hass(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

    async def test_options_flow_persists_changes(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"token": "old-token"},
            options={CONF_POLL_INTERVAL: 30},
        )
        entry.add_to_hass(hass)

        aioclient_mock.get(f"{BASE_URL}site", status=200, json={"devices": []})

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"token": "new-token", CONF_POLL_INTERVAL: 60}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {CONF_POLL_INTERVAL: 60}
        assert entry.data["token"] == "new-token"

    async def test_options_flow_rejects_bad_token(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"token": "t"},
            options={CONF_POLL_INTERVAL: 30},
        )
        entry.add_to_hass(hass)

        aioclient_mock.get(f"{BASE_URL}site", status=401)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"token": "bad", CONF_POLL_INTERVAL: 30}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}
        # Entry data should NOT have been updated.
        assert entry.data["token"] == "t"
