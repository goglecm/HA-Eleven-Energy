"""Tests for the :mod:`custom_components.eleven_energy_plus.diagnostics` module.

These tests cover three guarantees worth pinning down so a future refactor of
the diagnostics surface doesn't quietly drop them:

1. The diagnostics dict has a stable, documented shape (so users / scripts
   parsing it know what to expect).
2. Sensitive values (token, serial numbers, device ids) are redacted before
   the dict leaves the integration.
3. When the controller hasn't been set up (rare, but possible if a user
   downloads diagnostics during a failed setup), the dump still produces a
   well-formed dict rather than raising.
"""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.eleven_energy_plus.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"token": "super-secret-token"},
        options={CONF_POLL_INTERVAL: 30, CONF_DEVICE_LABEL: "My Label"},
        title="Eleven Energy Plus",
    )


def _device_payload(
    device_id: str = "abc", name: str = "North Sea 6 (6kW)"
) -> dict:
    return {
        "deviceId": device_id,
        "type": "hybridinverter",
        "name": name,
        "serialNumber": "SN-VERY-SECRET",
        "pv": {"power": 1.0},
        "battery": {"stateOfCharge": 80},
        "online": True,
    }


def _site_payload(*device_ids: str) -> dict:
    return {"devices": [_device_payload(d) for d in device_ids]}


class TestDiagnostics:
    """Snapshot-style coverage of the diagnostics dump."""

    async def test_dump_has_expected_top_level_shape(
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

        dump = await async_get_config_entry_diagnostics(hass, entry)

        assert set(dump) == {"integration", "entry", "controller", "raw_payloads"}
        assert dump["integration"]["domain"] == DOMAIN
        # The integration always ships a version string; we don't pin the
        # exact value here so the test doesn't churn on every bump, but it
        # must be a non-empty string that isn't the failure sentinel.
        assert isinstance(dump["integration"]["version"], str)
        assert dump["integration"]["version"] not in {"", "unknown"}

    async def test_token_is_redacted(
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

        dump = await async_get_config_entry_diagnostics(hass, entry)

        assert dump["entry"]["data"]["token"] != "super-secret-token"

        # The redacted form should not appear anywhere as a substring of
        # the serialised dump either, so we don't leak the token via some
        # other code path.
        rendered = repr(dump)
        assert "super-secret-token" not in rendered

    async def test_serial_and_device_ids_redacted_in_payloads(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=_site_payload("very-real-id"))
        aioclient_mock.get(
            f"{BASE_URL}devices/very-real-id",
            json=_device_payload("very-real-id"),
        )

        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        dump = await async_get_config_entry_diagnostics(hass, entry)

        rendered = repr(dump)
        assert "very-real-id" not in rendered
        assert "SN-VERY-SECRET" not in rendered

    async def test_user_facing_label_not_redacted(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        """Product / label strings are kept so they can be inspected."""
        aioclient_mock.get(
            f"{BASE_URL}site",
            json={"devices": [_device_payload("a", name="North Sea 6 (6kW)")]},
        )
        aioclient_mock.get(
            f"{BASE_URL}devices/a", json=_device_payload("a")
        )

        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        dump = await async_get_config_entry_diagnostics(hass, entry)

        site_payload = dump["raw_payloads"]["site"]
        assert site_payload is not None
        assert site_payload["devices"][0]["name"] == "North Sea 6 (6kW)"

    async def test_controller_block_reflects_options(
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

        dump = await async_get_config_entry_diagnostics(hass, entry)

        controller = dump["controller"]
        assert controller["loaded"] is True
        assert controller["poll_interval_seconds"] == 30
        assert controller["device_label_override"] == "My Label"
        # Device ids are wrapped in dicts so the redactor can rewrite the
        # actual id while still preserving the count for reviewers.
        assert isinstance(controller["known_devices"], list)
        assert len(controller["known_devices"]) == 1
        assert "deviceId" in controller["known_devices"][0]

    async def test_dump_without_loaded_controller(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
    ) -> None:
        """No controller in ``hass.data`` should not raise."""
        # Entry is not added/setup, so hass.data[DOMAIN] doesn't exist.
        dump = await async_get_config_entry_diagnostics(hass, entry)

        assert dump["controller"] == {"loaded": False}
        assert dump["raw_payloads"] == {"site": None, "devices": {}}, (
            "When the controller is absent, the raw_payloads slot is "
            "left as a placeholder dict rather than the loaded-shape "
            "list, so consumers can distinguish 'not yet polled' from "
            "'polled with no devices'."
        )
        # Even without a controller, the token should still be redacted -
        # entry-data redaction is independent of controller availability.
        assert dump["entry"]["data"]["token"] != "super-secret-token"


class TestRedactionSet:
    """Lock the redaction key set so adding more keys is a deliberate act."""

    def test_to_redact_is_a_frozenset(self) -> None:
        # ``frozenset`` is intentional: it prevents accidental in-place
        # mutation from a downstream module.
        assert isinstance(TO_REDACT, frozenset)

    def test_to_redact_covers_critical_keys(self) -> None:
        # We don't pin the exact set (so additions don't require a test
        # change), only that the critical ones are present.
        for required in ("token", "serialNumber", "deviceId"):
            assert required in TO_REDACT, (
                f"{required!r} must be in diagnostics TO_REDACT"
            )

    def test_to_redact_handles_snake_and_camel_case(self) -> None:
        # The site API uses camelCase, but some HA internals and our own
        # options keys are snake_case. Cover both spellings of the same
        # concept to avoid surprises when those callsites cross-pollinate.
        assert "serial_number" in TO_REDACT
        assert "device_id" in TO_REDACT


def test_diagnostics_module_imports_cleanly() -> None:
    """Importing the module must not require an event loop / hass."""
    # ``async_get_config_entry_diagnostics`` is the public surface.
    from custom_components.eleven_energy_plus import diagnostics as _diag

    assert callable(_diag.async_get_config_entry_diagnostics)
    assert hasattr(_diag, "TO_REDACT")


def test_read_integration_version_returns_known_value() -> None:
    """The version helper should resolve to the value in manifest.json."""
    from custom_components.eleven_energy_plus.diagnostics import (
        _read_integration_version,
    )

    version = _read_integration_version()
    assert version not in {"", "unknown"}
    # Manifest is checked into the repo so we can pin to the major; bumping
    # the integer prefix would be a deliberate breaking-version change and
    # would deserve a test update anyway.
    assert version.startswith("1.")


def test_read_integration_version_handles_missing_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing/malformed manifest must not crash diagnostics."""
    import custom_components.eleven_energy_plus.diagnostics as diag_mod

    # Force the manifest read to look at a non-existent path.
    fake_file = tmp_path / "diagnostics.py"
    fake_file.write_text("")
    monkeypatch.setattr(diag_mod, "__file__", str(fake_file))

    assert diag_mod._read_integration_version() == "unknown"
