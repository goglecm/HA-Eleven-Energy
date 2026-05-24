"""Lifecycle tests for the Eleven Energy integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    CONF_POLL_INTERVAL,
    DOMAIN,
)


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"token": "tok"},
        options={CONF_POLL_INTERVAL: 30},
        title="Eleven Energy",
    )


def _device_payload(device_id: str = "abc") -> dict:
    return {
        "deviceId": device_id,
        "type": "hybridinverter",
        "name": "Inverter A",
        "serialNumber": "SN-A",
        "pv": {"power": 1.0},
        "battery": {"stateOfCharge": 80},
        "online": True,
    }


def _site_payload(*device_ids: str) -> dict:
    return {"devices": [_device_payload(d) for d in device_ids]}


class TestSetupAndUnload:
    """Happy path lifecycle plus retry-on-failure behaviour."""

    async def test_setup_and_unload_happy_path(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=_site_payload("abc"))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=_device_payload("abc")
        )

        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        # Controller stored in hass.data
        assert DOMAIN in hass.data
        assert "controller" in hass.data[DOMAIN]

        # Entities should exist on the entity registry.
        states = hass.states.async_all()
        ids = {s.entity_id for s in states}
        assert any("pv_power" in eid for eid in ids)
        assert any("state_of_charge" in eid for eid in ids)
        assert any("system_online" in eid for eid in ids)

        # Now unload cleanly.
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        # Controller is removed.
        assert hass.data.get(DOMAIN, {}).get("controller") is None

    async def test_setup_retries_on_no_devices(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        # Empty site -> ConfigEntryNotReady -> entry stays in SETUP_RETRY.
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": []})

        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.SETUP_RETRY

    async def test_setup_retries_on_5xx(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=503)

        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.SETUP_RETRY

    async def test_platform_forward_failure_rolls_back(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        """If platform setup raises, the controller should be torn down."""
        aioclient_mock.get(f"{BASE_URL}site", json=_site_payload("abc"))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=_device_payload("abc")
        )

        entry.add_to_hass(hass)

        with patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            side_effect=RuntimeError("boom"),
        ):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        # Setup failed - data should be cleaned up.
        assert hass.data.get(DOMAIN, {}).get("controller") is None
        # Entry should be in ERROR state, not LOADED.
        assert entry.state is not ConfigEntryState.LOADED


class TestOptionsUpdated:
    """Options-flow updates either reload (token change) or ping the poller."""

    async def test_token_change_triggers_reload(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=_site_payload("abc"))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=_device_payload("abc")
        )

        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        reload_called = False

        async def fake_reload(entry_id: str) -> bool:
            nonlocal reload_called
            reload_called = True
            return True

        with patch.object(
            hass.config_entries, "async_reload", side_effect=fake_reload
        ):
            hass.config_entries.async_update_entry(
                entry, data={"token": "new-token"}
            )
            await hass.async_block_till_done()

        assert reload_called

    async def test_options_only_change_pings_controller(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=_site_payload("abc"))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=_device_payload("abc")
        )

        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        controller = hass.data[DOMAIN]["controller"]

        notifications: list[bool] = []
        controller.add_option_listener(lambda: notifications.append(True))

        # Same token, different option - controller should be notified and
        # the new poll interval should be visible.
        hass.config_entries.async_update_entry(
            entry, options={CONF_POLL_INTERVAL: 45}
        )
        await hass.async_block_till_done()

        assert notifications, "option listener was never invoked"
        assert controller.poll_interval == 45

    async def test_device_label_override_change_triggers_reload(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        """Toggling the device-label override forces a full reload."""
        from custom_components.eleven_energy_plus.const import CONF_DEVICE_LABEL

        aioclient_mock.get(f"{BASE_URL}site", json=_site_payload("abc"))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=_device_payload("abc")
        )

        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        reload_called = False

        async def fake_reload(entry_id: str) -> bool:
            nonlocal reload_called
            reload_called = True
            return True

        with patch.object(
            hass.config_entries, "async_reload", side_effect=fake_reload
        ):
            hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, CONF_DEVICE_LABEL: "Custom"},
            )
            await hass.async_block_till_done()

        assert reload_called

    async def test_device_label_override_whitespace_does_not_reload(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        """Saving the same override with extra spaces does not trigger a reload."""
        from custom_components.eleven_energy_plus.const import CONF_DEVICE_LABEL

        aioclient_mock.get(f"{BASE_URL}site", json=_site_payload("abc"))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=_device_payload("abc")
        )

        # Pre-seed the entry with an override so the controller snapshots it
        # at construction time.
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"token": "test-token"},
            options={CONF_POLL_INTERVAL: 30, CONF_DEVICE_LABEL: "Custom"},
            title="Eleven Energy Plus",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        reload_called = False

        async def fake_reload(entry_id: str) -> bool:
            nonlocal reload_called
            reload_called = True
            return True

        with patch.object(
            hass.config_entries, "async_reload", side_effect=fake_reload
        ):
            # Same effective value (after strip) - the reload check compares
            # against the controller's construction-time snapshot.
            hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, CONF_DEVICE_LABEL: "  Custom  "},
            )
            await hass.async_block_till_done()

        assert not reload_called


class TestServiceResponse:
    """The set_work_mode_* services return a structured response dict."""

    async def test_service_returns_response_dict(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=_site_payload("abc"))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=_device_payload("abc")
        )
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode",
            status=200,
            json={"accepted": True},
        )

        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        response = await hass.services.async_call(
            DOMAIN,
            "set_work_mode_force_charge",
            {"target_power": 3.5, "target_percent": 80},
            blocking=True,
            return_response=True,
        )
        assert response["success"] is True
        assert response["status"] == 200
        assert response["device_id"] == "abc"
        assert response["work_mode"] == "forceCharge"
        assert response["params"] == {
            "target_power": 3.5,
            "target_percent": 80.0,
        }
        assert response["error"] is None

    async def test_service_returns_error_without_controller(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Calling the service before setup yields a structured error."""
        # Bootstrap async_setup so services are registered without an entry.
        from custom_components.eleven_energy_plus import async_setup

        await async_setup(hass, {})

        response = await hass.services.async_call(
            DOMAIN,
            "set_work_mode_self_consumption",
            {},
            blocking=True,
            return_response=True,
        )
        assert response["success"] is False
        assert response["error"] == "integration not loaded"
        assert response["status"] is None
