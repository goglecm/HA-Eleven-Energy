"""Static checks on the integration's metadata and translation files.

These catch the class of bug that only shows up in the UI or in CI
validators: a translation key the code references but ``en.json`` lacks, a
``services.yaml`` that hassfest rejects, selector ranges that disagree with
the validation the actions actually perform.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest
import yaml

from custom_components.eleven_energy_plus import const, hybrid_inverter, services
from custom_components.eleven_energy_plus.const import ErrorKind
from custom_components.eleven_energy_plus.device_action import ACTION_TYPES
from custom_components.eleven_energy_plus.services import SERVICE_FIELDS

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "custom_components" / "eleven_energy_plus"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def translations() -> dict[str, Any]:
    return _load_json(PACKAGE / "translations" / "en.json")


@pytest.fixture(scope="module")
def services_yaml() -> dict[str, Any]:
    return yaml.safe_load((PACKAGE / "services.yaml").read_text(encoding="utf-8"))


class TestManifest:
    """The manifest carries what HA 2026.11, hassfest and the Marketplace expect."""

    @pytest.mark.covers("FC-STATIC-04")
    def test_manifest_fields(self) -> None:
        manifest = _load_json(PACKAGE / "manifest.json")
        assert manifest["domain"] == const.DOMAIN
        assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
        assert manifest["categories"] == ["energy.solar_batteries"]
        assert manifest["single_config_entry"] is True
        assert manifest["config_flow"] is True
        assert manifest["iot_class"] == "cloud_polling"
        assert manifest["integration_type"] == "hub"
        # "my" would force the frontend stack to load; the OAuth
        # implementation returns the my.home-assistant.io redirect itself.
        assert "my" not in manifest["dependencies"]
        assert "device_automation" in manifest["dependencies"]

    @pytest.mark.covers("FC-STATIC-04")
    def test_hacs_json(self) -> None:
        hacs = _load_json(ROOT / "hacs.json")
        assert hacs["name"] == "Eleven Energy Plus"
        assert hacs["homeassistant"] == "2026.3.0"

    def test_brand_images_present(self) -> None:
        for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
            assert (PACKAGE / "brand" / name).is_file()


class TestServicesYaml:
    """``services.yaml`` follows hassfest's current rules."""

    @pytest.mark.covers("FC-STATIC-01")
    def test_same_services_as_code(self, services_yaml: dict[str, Any]) -> None:
        assert set(services_yaml) == set(SERVICE_FIELDS)

    @pytest.mark.covers("FC-STATIC-01")
    def test_no_target_declared(self, services_yaml: dict[str, Any]) -> None:
        """hassfest refuses device filters on target; a target would also be required."""
        for service, definition in services_yaml.items():
            assert "target" not in definition, service

    def test_inverter_is_a_device_selector_field(self, services_yaml: dict[str, Any]) -> None:
        for service, definition in services_yaml.items():
            device_field = definition["fields"]["device_id"]
            assert device_field["required"] is False
            assert device_field["selector"]["device"]["integration"] == const.DOMAIN, service

    def test_fields_match_schemas(self, services_yaml: dict[str, Any]) -> None:
        for service, definition in services_yaml.items():
            fields = {k: v for k, v in definition["fields"].items() if k != "device_id"}
            assert set(fields) == set(SERVICE_FIELDS[service]), service
            for field, required in SERVICE_FIELDS[service].items():
                assert fields[field]["required"] is required, (service, field)

    def test_selector_ranges_match_constants(self, services_yaml: dict[str, Any]) -> None:
        for definition in services_yaml.values():
            for field, spec in definition["fields"].items():
                number = spec.get("selector", {}).get("number")
                if number is None:
                    continue
                if spec["selector"]["number"].get("unit_of_measurement") == "kW":
                    assert number["min"] == const.MIN_POWER_KW
                    assert number["max"] == const.UI_MAX_POWER_KW <= const.MAX_POWER_KW
                elif field == "target_minutes":
                    assert number["min"] == const.MIN_TARGET_MINUTES
                    assert number["max"] == const.MAX_TARGET_MINUTES
                else:
                    assert (number["min"], number["max"]) == (0, 100), field

    @pytest.mark.covers("FC-STATIC-01")
    def test_no_response_key(self, services_yaml: dict[str, Any]) -> None:
        """hassfest rejects ``response``; response support is declared in code."""
        for service, definition in services_yaml.items():
            assert set(definition) == {"fields"}, service

    async def test_actions_support_optional_responses(self, hass) -> None:
        from homeassistant.core import SupportsResponse
        from homeassistant.setup import async_setup_component

        assert await async_setup_component(hass, const.DOMAIN, {})
        for service in SERVICE_FIELDS:
            assert (
                hass.services.supports_response(const.DOMAIN, service)
                is SupportsResponse.OPTIONAL
            )


class TestTranslations:
    """Every key the code can surface has an English string."""

    @pytest.mark.covers("FC-STATIC-02")
    def test_services_and_fields(
        self, translations: dict[str, Any], services_yaml: dict[str, Any]
    ) -> None:
        for service, definition in services_yaml.items():
            entry = translations["services"][service]
            assert entry["name"] and entry["description"]
            for field in definition["fields"]:
                assert entry["fields"][field]["name"], (service, field)
                assert entry["fields"][field]["description"], (service, field)

    def test_device_automation(self, translations: dict[str, Any]) -> None:
        automation = translations["device_automation"]
        assert set(automation["action_type"]) == set(ACTION_TYPES)
        all_fields = {field for fields in SERVICE_FIELDS.values() for field in fields}
        assert set(automation["extra_fields"]) == all_fields

    def test_exceptions_used_by_services(self, translations: dict[str, Any]) -> None:
        """Each error kind raises its own translated message naming the action."""
        keys = [key for _exception, key in services._RAISED.values()]
        assert len(keys) == len(set(keys)) == len(ErrorKind), "one message per error kind"
        assert set(services._RAISED) == set(ErrorKind)
        for key in keys:
            message = translations["exceptions"][key]["message"]
            assert "{service}" in message

    @pytest.mark.covers("FC-STATIC-02")
    def test_config_steps_and_menus(self, translations: dict[str, Any]) -> None:
        steps = translations["config"]["step"]
        for step in ("user", "token", "reauth_account", "reauth_confirm", "reconfigure"):
            assert step in steps
        for menu in ("user", "reconfigure"):
            assert set(steps[menu]["menu_options"]) == {"pick_implementation", "token"}
        aborts = translations["config"]["abort"]
        for reason in (
            "single_instance_allowed",
            "reauth_successful",
            "reconfigure_successful",
            "missing_configuration",
            "oauth_error",
            "oauth_failed",
            "user_rejected_authorize",
        ):
            assert reason in aborts
        for outcome in ("cannot_connect", "invalid_auth", "no_inverters", "different_site", "unknown"):
            assert outcome in translations["config"]["error"], "a token is refused with a form error"
            assert outcome in aborts, "a sign-in is refused by ending the flow"
        assert set(translations["options"]["error"]) == {"poll_interval_invalid", "label_invalid"}

    @pytest.mark.covers("FC-STATIC-02")
    def test_entity_translation_keys(self, translations: dict[str, Any]) -> None:
        sensors = translations["entity"]["sensor"]
        binary = translations["entity"]["binary_sensor"]
        for meta in hybrid_inverter._PREDEFINED_DEFS.values():
            key = meta.get("translation_key", meta["entity_type"].lower())
            target = binary if meta["kind"] == "binary_sensor" else sensors
            assert key in target, key
        for fields in hybrid_inverter._INDEXED_LISTS.values():
            for meta in fields.values():
                assert "{index}" in sensors[meta["translation_key"]]["name"]
        for group in hybrid_inverter._PHASE_GROUPS:
            for field in hybrid_inverter._PHASE_FIELDS:
                assert "{phase}" in sensors[f"{group}_phase_{field}"]["name"]
        for meta in hybrid_inverter._RATING_FIELDS.values():
            assert meta["translation_key"] in sensors
        assert "api_connection" in binary
        assert "poll_interval" in translations["entity"]["number"]

    def test_translation_keys_are_valid(self, translations: dict[str, Any]) -> None:
        for platform in translations["entity"].values():
            for key in platform:
                assert re.fullmatch(r"[a-z0-9_]+", key), key
