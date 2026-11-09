"""Lifecycle and action tests for the Eleven Energy Plus integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DOMAIN,
    OAUTH_TOKEN_URL,
    UNAVAILABLE_AFTER_FAILED_POLLS,
)
from custom_components.eleven_energy_plus.controller import Controller
from custom_components.eleven_energy_plus.services import SERVICE_NAMES

from .common import (
    DEVICE_ID,
    MODEL_SINGLE_PHASE,
    api_key_entry,
    device_payload,
    mock_api,
    setup_entry,
    site_summary,
)

POST_URL = f"{BASE_URL}devices/{DEVICE_ID}/operatingMode"


def _our_device(hass: HomeAssistant, entry) -> dr.DeviceEntry:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    return devices[0]


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any] | None = None, *, response: bool
) -> Any:
    return await hass.services.async_call(
        DOMAIN, service, data or {}, blocking=True, return_response=response
    )


class TestSetupAndUnload:
    """Happy path lifecycle plus retry-on-failure behaviour."""

    async def test_setup_and_unload_happy_path(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(
            aioclient_mock,
            devices={DEVICE_ID: device_payload(model=MODEL_SINGLE_PHASE)},
        )
        entry = api_key_entry()
        assert await setup_entry(hass, entry)

        assert entry.state is ConfigEntryState.LOADED
        assert isinstance(entry.runtime_data, Controller)

        ids = {state.entity_id for state in hass.states.async_all()}
        assert "sensor.abc_pv_power" in ids
        assert "sensor.abc_state_of_charge" in ids
        assert "binary_sensor.abc_system_online" in ids
        assert "number.abc_poll_interval" in ids
        assert "sensor.abc_mppt_1_power" in ids
        assert hass.states.get("sensor.abc_pv_power").state == "1.5"
        assert hass.states.get("binary_sensor.abc_system_online").state == "on"

        device = _our_device(hass, entry)
        assert device.name == "Eleven Energy Plus Mediterranean Sea 12"
        assert device.model == "Mediterranean Sea 12"
        assert device.manufacturer == "Eleven Energy"

        controller = entry.runtime_data
        assert controller.poller_task is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert controller.poller_task is None
        assert controller._shutdown.is_set()

    async def test_setup_retries_on_no_devices(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": []})
        entry = api_key_entry()
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_RETRY

    async def test_setup_retries_on_5xx(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=503)
        entry = api_key_entry()
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_RETRY

    async def test_setup_retries_on_network_error(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", exc=TimeoutError())
        entry = api_key_entry()
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_RETRY

    @pytest.mark.parametrize("status", [401, 403])
    async def test_rejected_credential_at_setup_starts_reauth(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=status)
        entry = api_key_entry()
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        flows = [
            flow
            for flow in hass.config_entries.flow.async_progress()
            if flow["context"]["source"] == SOURCE_REAUTH
        ]
        assert len(flows) == 1
        assert flows[0]["step_id"] == "reauth_confirm"

    async def test_missing_token_starts_reauth(self, hass: HomeAssistant) -> None:
        entry = api_key_entry(data={"token": "  "})
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_ERROR

    async def test_platform_forward_failure_rolls_back(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        entry = api_key_entry()
        entry.add_to_hass(hass)
        with patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            side_effect=RuntimeError("boom"),
        ):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        assert entry.state is not ConfigEntryState.LOADED
        assert entry.runtime_data._terminated

    async def test_yaml_configuration_is_not_supported(self, hass: HomeAssistant) -> None:
        assert await async_setup_component(hass, DOMAIN, {})
        for service in SERVICE_NAMES:
            assert hass.services.has_service(DOMAIN, service)


class TestReloadAndOptions:
    """Reloads keep the actions; options apply live or reload as appropriate."""

    async def test_services_survive_reload(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        """Regression: every action used to disappear after a reload."""
        mock_api(aioclient_mock)
        aioclient_mock.post(POST_URL, status=200)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        for service in SERVICE_NAMES:
            assert hass.services.has_service(DOMAIN, service), service
        response = await _call(hass, "set_work_mode_reset", response=True)
        assert response["success"] is True

    async def test_services_stay_registered_after_unload(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        response = await _call(hass, "set_work_mode_reset", response=True)
        assert response["success"] is False
        assert response["error"] == "integration not loaded"
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, "set_work_mode_reset", response=False)
        assert err.value.translation_key == "not_loaded"

    async def test_interval_option_applies_live(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)
        controller = entry.runtime_data
        pings: list[bool] = []
        controller.add_option_listener(lambda: pings.append(True))

        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_POLL_INTERVAL: 45}
        )
        await hass.async_block_till_done()

        assert entry.runtime_data is controller, "no reload for an interval change"
        assert pings
        assert controller.poll_interval == 45
        assert hass.states.get("number.abc_poll_interval").state == "45.0"

    async def test_label_override_change_reloads(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)
        controller = entry.runtime_data

        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_DEVICE_LABEL: "My Inverter"}
        )
        await hass.async_block_till_done()

        assert entry.runtime_data is not controller
        assert entry.state is ConfigEntryState.LOADED
        assert _our_device(hass, entry).name == "Eleven Energy Plus My Inverter"

    async def test_credential_data_change_alone_does_not_reload(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        """Token refreshes rewrite entry data; that must never reload anything."""
        mock_api(aioclient_mock)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)
        controller = entry.runtime_data
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            hass.config_entries.async_update_entry(entry, data={"token": "other"})
            await hass.async_block_till_done()
        reload.assert_not_called()
        assert entry.runtime_data is controller

    async def test_number_entity_updates_options(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": "number.abc_poll_interval", "value": 60},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert entry.options[CONF_POLL_INTERVAL] == 60
        assert entry.runtime_data.poll_interval == 60


class TestAvailability:
    """Entity states follow the inverter's availability."""

    async def test_entities_go_unavailable_and_recover(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)
        controller = entry.runtime_data

        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS):
            await controller.poll_devices()
        await hass.async_block_till_done()
        assert hass.states.get("sensor.abc_pv_power").state == STATE_UNAVAILABLE
        assert hass.states.get("binary_sensor.abc_system_online").state == STATE_UNAVAILABLE
        # The poll-interval setting stays usable during an outage.
        assert hass.states.get("number.abc_poll_interval").state != STATE_UNAVAILABLE

        aioclient_mock.clear_requests()
        aioclient_mock.get(
            f"{BASE_URL}devices/{DEVICE_ID}", json=device_payload(pv={"power": 2.5})
        )
        await controller.poll_devices()
        await hass.async_block_till_done()
        assert hass.states.get("sensor.abc_pv_power").state == "2.5"

    async def test_rejected_credential_at_runtime(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)

        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=401)
        await entry.runtime_data.poll_devices()
        await hass.async_block_till_done()

        assert hass.states.get("sensor.abc_pv_power").state == STATE_UNAVAILABLE
        assert any(
            flow["context"]["source"] == SOURCE_REAUTH
            for flow in hass.config_entries.flow.async_progress()
        )


class TestServiceResponse:
    """The set_work_mode_* actions validate, respond, and raise when unheard."""

    async def test_success_with_response(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        aioclient_mock.post(POST_URL, status=200, json={"accepted": True})
        assert await setup_entry(hass, api_key_entry())

        response = await _call(
            hass,
            "set_work_mode_force_charge",
            {"target_power": 3.5, "target_percent": 80},
            response=True,
        )
        assert response == {
            "success": True,
            "status": 200,
            "device_id": DEVICE_ID,
            "work_mode": "forceCharge",
            "params": {"target_power": 3.5, "target_percent": 80.0},
            "attempts": 1,
            "error": None,
        }

    async def test_success_without_response(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        aioclient_mock.post(POST_URL, status=200)
        assert await setup_entry(hass, api_key_entry())
        assert await _call(hass, "set_work_mode_pv_export", response=False) is None

    async def test_api_failure_raises_without_response(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff,
    ) -> None:
        """Safety: a failed command must not pass silently in an automation."""
        mock_api(aioclient_mock)
        aioclient_mock.post(POST_URL, status=500)
        assert await setup_entry(hass, api_key_entry())
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, "set_work_mode_reset", response=False)
        assert not isinstance(err.value, ServiceValidationError)
        assert err.value.translation_key == "work_mode_failed"
        assert err.value.translation_placeholders["error"] == "API returned status 500"

    async def test_api_failure_returned_with_response(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff,
    ) -> None:
        mock_api(aioclient_mock)
        aioclient_mock.post(POST_URL, status=500)
        assert await setup_entry(hass, api_key_entry())
        response = await _call(hass, "set_work_mode_reset", response=True)
        assert response["success"] is False
        assert response["attempts"] == 5

    @pytest.mark.parametrize(
        ("service", "data"),
        [
            ("set_work_mode_force_charge", {"target_power": "nan", "target_percent": 50}),
            ("set_work_mode_force_charge", {"target_power": 60, "target_percent": 50}),
            ("set_work_mode_force_charge", {"target_power": 3, "target_percent": 150}),
            ("set_work_mode_force_charge", {"target_power": True, "target_percent": 50}),
            ("set_work_mode_force_charge", {"target_power": 3}),
            ("set_work_mode_target_soc", {"target_soc": 50, "target_minutes": "inf"}),
            ("set_work_mode_target_soc", {"target_soc": 50, "target_minutes": 0}),
            ("set_work_mode_target_soc", {"target_soc": 50, "target_minutes": 60.5}),
            ("set_work_mode_idle_battery", {"allow_charging": "maybe"}),
            ("set_work_mode_reset", {"unexpected": 1}),
            (
                "set_work_mode_self_consumption",
                {"percent_to_battery": 10, "percentage_to_battery": 20},
            ),
        ],
    )
    async def test_invalid_input_is_rejected_before_sending(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        service: str,
        data: dict[str, Any],
    ) -> None:
        mock_api(aioclient_mock)
        aioclient_mock.post(POST_URL, status=200)
        assert await setup_entry(hass, api_key_entry())
        posts_before = sum(1 for call in aioclient_mock.mock_calls if call[0] == "POST")
        with pytest.raises((vol.Invalid, ServiceValidationError)):
            await _call(hass, service, data, response=True)
        posts_after = sum(1 for call in aioclient_mock.mock_calls if call[0] == "POST")
        assert posts_after == posts_before

    async def test_legacy_percentage_alias(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        aioclient_mock.post(POST_URL, status=200)
        assert await setup_entry(hass, api_key_entry())
        response = await _call(
            hass,
            "set_work_mode_self_consumption",
            {"percentage_to_battery": 40},
            response=True,
        )
        assert response["params"] == {"percent_to_battery": 40.0}
        assert aioclient_mock.mock_calls[-1][2]["targetExcessPc"] == 40.0

    async def test_numeric_strings_from_templates_are_accepted(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        aioclient_mock.post(POST_URL, status=200)
        assert await setup_entry(hass, api_key_entry())
        response = await _call(
            hass,
            "set_work_mode_target_soc",
            {"target_soc": "80", "target_minutes": "120.0", "max_charge_power": "4.5"},
            response=True,
        )
        assert response["success"] is True
        assert aioclient_mock.mock_calls[-1][2] == {
            "workMode": "targetSoc",
            "targetSoc": 80.0,
            "targetMinutes": 120,
            "maxChargeRate": 4.5,
        }

    async def test_target_problems_raise_validation_error(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        assert await setup_entry(hass, api_key_entry())
        with pytest.raises(ServiceValidationError) as err:
            await _call(
                hass, "set_work_mode_reset", {"device_id": "not-a-device"}, response=False
            )
        assert err.value.translation_key == "invalid_request"

    async def test_device_field_targets_the_right_inverter(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        devices = {"a": device_payload("a"), "b": device_payload("b")}
        mock_api(aioclient_mock, devices=devices)
        aioclient_mock.post(f"{BASE_URL}devices/b/operatingMode", status=200)
        entry = api_key_entry()
        assert await setup_entry(hass, entry)

        with pytest.raises(ServiceValidationError):
            await _call(hass, "set_work_mode_reset", response=False)

        dev_reg = dr.async_get(hass)
        device_b = next(
            device
            for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
            if (DOMAIN, "b") in device.identifiers
        )
        response = await _call(
            hass, "set_work_mode_reset", {"device_id": device_b.id}, response=True
        )
        assert response["device_id"] == "b"
        assert response["success"] is True

        entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "b_pv_power")
        response = await _call(
            hass, "set_work_mode_reset", {"entity_id": entity_id}, response=True
        )
        assert response["device_id"] == "b"

    async def test_not_loaded_returns_or_raises(self, hass: HomeAssistant) -> None:
        assert await async_setup_component(hass, DOMAIN, {})
        response = await _call(hass, "set_work_mode_self_consumption", response=True)
        assert response["success"] is False
        assert response["error"] == "integration not loaded"
        assert response["status"] is None
        with pytest.raises(HomeAssistantError):
            await _call(hass, "set_work_mode_self_consumption", response=False)


def _sign_in_entry(expires_at: float) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Eleven Energy Plus",
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 28800,
                "expires_at": expires_at,
                "token_type": "Bearer",
            },
        },
        options={CONF_POLL_INTERVAL: 30},
    )


class TestSignInEntries:
    """Account sign-in entries use (and refresh) Home Assistant's OAuth token."""

    async def test_setup_uses_access_token(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        entry = _sign_in_entry(expires_at=9_999_999_999)
        assert await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.uses_account_sign_in
        site_call = aioclient_mock.mock_calls[0]
        assert site_call[3]["Authorization"] == "Bearer access-1"

    async def test_expired_token_is_refreshed_without_reload(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        mock_api(aioclient_mock)
        aioclient_mock.post(
            OAUTH_TOKEN_URL,
            json={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 28800,
                "token_type": "Bearer",
            },
        )
        entry = _sign_in_entry(expires_at=0)
        assert await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.data["token"]["access_token"] == "access-2"
        gets = [call for call in aioclient_mock.mock_calls if call[0] == "GET"]
        assert gets[0][3]["Authorization"] == "Bearer access-2"

        # A refresh at runtime rewrites entry data; nothing may reload.
        controller = entry.runtime_data
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "token": {**entry.data["token"], "expires_at": 0}}
        )
        await hass.async_block_till_done()
        await controller.poll_devices()
        await hass.async_block_till_done()
        assert entry.runtime_data is controller
        assert entry.state is ConfigEntryState.LOADED

    async def test_refused_refresh_starts_reauth(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.post(OAUTH_TOKEN_URL, status=400, json={"error": "invalid_grant"})
        entry = _sign_in_entry(expires_at=0)
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        flows = [
            flow
            for flow in hass.config_entries.flow.async_progress()
            if flow["context"]["source"] == SOURCE_REAUTH
        ]
        assert len(flows) == 1
        assert flows[0]["step_id"] == "reauth_account"


class TestRegistryMigration:
    """Pre-2.0 positional entities move to the curated ones at setup."""

    async def test_positional_entities_are_migrated_and_phantoms_removed(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        entry = api_key_entry()
        entry.add_to_hass(hass)
        ent_reg = er.async_get(hass)

        def _old(domain: str, unique_id: str) -> str:
            return ent_reg.async_get_or_create(
                domain, DOMAIN, unique_id, config_entry=entry, suggested_object_id=unique_id
            ).entity_id

        old_mppt_1 = _old("sensor", "abc_pv_mppts_0_power")
        old_mppt_3 = _old("sensor", "abc_pv_mppts_2_power")
        old_mppt_index = _old("sensor", "abc_pv_mppts_0_index")
        old_battery = _old("sensor", "abc_battery_batteries_0_stateOfCharge")
        old_model_flag = _old("binary_sensor", "abc_model_threePhase")
        old_rating = _old("sensor", "abc_model_maxAcPower")
        old_phase = _old("sensor", "abc_grid_phases_L2_power")
        unrelated = _old("sensor", "abc_someOtherField")

        mock_api(
            aioclient_mock,
            devices={
                DEVICE_ID: device_payload(
                    model=MODEL_SINGLE_PHASE,
                    mppts=[{"index": i, "power": 0.1 * i} for i in (1, 2, 3)],
                    batteries=[{"index": 1, "stateOfCharge": 81}],
                )
            },
            summaries=[site_summary(model=MODEL_SINGLE_PHASE)],
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        migrated = ent_reg.async_get(old_mppt_1)
        assert migrated is not None and migrated.unique_id == "abc_mppt_1_power"
        assert hass.states.get(old_mppt_1).state == "0.1"
        assert ent_reg.async_get(old_battery).unique_id == "abc_battery_1_raw_state_of_charge"
        assert ent_reg.async_get(old_rating).unique_id == "abc_model_maxAcPower"
        for removed in (old_mppt_3, old_mppt_index, old_model_flag, old_phase):
            assert ent_reg.async_get(removed) is None, removed
        assert ent_reg.async_get(unrelated) is not None

        # Idempotent: a reload finds nothing left to migrate.
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert ent_reg.async_get(old_mppt_1).unique_id == "abc_mppt_1_power"

    async def test_nothing_removed_without_known_hardware(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        entry = api_key_entry()
        entry.add_to_hass(hass)
        ent_reg = er.async_get(hass)
        kept = ent_reg.async_get_or_create(
            "sensor", DOMAIN, "abc_pv_mppts_2_power", config_entry=entry
        ).entity_id
        aioclient_mock.get(
            f"{BASE_URL}site", json={"devices": [site_summary()]}
        )
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert ent_reg.async_get(kept) is not None
