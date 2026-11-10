"""Tests for services.py: action registration, schemas and failure semantics."""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus import services
from custom_components.eleven_energy_plus.const import BASE_URL, DOMAIN, ErrorKind

from .common import DEVICE_ID, api_key_entry, mock_api, setup_entry

POST_URL = f"{BASE_URL}devices/{DEVICE_ID}/operatingMode"


async def test_registration_is_idempotent(hass: HomeAssistant) -> None:
    services.async_setup_services(hass)
    handlers = {name: hass.services.async_services_for_domain(DOMAIN)[name] for name in services.SERVICE_NAMES}
    services.async_setup_services(hass)
    for name in services.SERVICE_NAMES:
        assert hass.services.async_services_for_domain(DOMAIN)[name] is handlers[name]
        assert hass.services.supports_response(DOMAIN, name) is SupportsResponse.OPTIONAL


@pytest.mark.covers("FC-SVC-05", "FC-SVC-06")
@pytest.mark.parametrize("key", ["percent_to_battery", "percentage_to_battery"])
async def test_the_field_and_its_alias_both_work(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, key: str
) -> None:
    mock_api(aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    assert await setup_entry(hass, api_key_entry())
    result = await hass.services.async_call(
        DOMAIN, "set_work_mode_self_consumption", {key: 40}, blocking=True, return_response=True
    )
    assert result["success"] is True
    assert result["params"] == {"percent_to_battery": 40.0}


@pytest.mark.covers("FC-SVC-05")
async def test_the_alias_and_the_field_cannot_both_be_given(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    assert await setup_entry(hass, api_key_entry())
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(
            DOMAIN,
            "set_work_mode_self_consumption",
            {"percent_to_battery": 50, "percentage_to_battery": 40},
            blocking=True,
        )


@pytest.mark.covers("FC-SVC-12")
async def test_not_loaded_result_has_the_full_shape(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.services.async_call(
        DOMAIN, "set_work_mode_reset", {}, blocking=True, return_response=True
    )
    assert result == {
        "success": False,
        "status": None,
        "device_id": None,
        "work_mode": None,
        "params": {},
        "attempts": 0,
        "error": "integration not loaded",
        "error_kind": ErrorKind.NOT_LOADED,
    }


@pytest.mark.covers("FC-SVC-08")
def test_every_error_kind_raises_its_own_message() -> None:
    assert set(services._RAISED) == set(ErrorKind)
    keys = [key for _cls, key in services._RAISED.values()]
    assert len(set(keys)) == len(keys)
    validation = [kind for kind, (cls, _key) in services._RAISED.items() if cls is ServiceValidationError]
    assert validation == [ErrorKind.INVALID_REQUEST], "only bad input is the caller's to fix"


_FIELD_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=8),
    st.lists(st.integers(), max_size=2),
)


@pytest.mark.covers("FC-SVC-01", "FC-SVC-02")
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(
    service=st.sampled_from(services.SERVICE_NAMES),
    data=st.dictionaries(st.sampled_from(sorted(services.FIELD_VALIDATORS)), _FIELD_VALUES, max_size=4),
)
def test_random_input_is_validated_or_rejected_never_passed_through(
    service: str, data: dict[str, Any]
) -> None:
    """Whatever the schema accepts is exactly what the fields allow."""
    schema = services.build_schema(service)
    try:
        validated = schema(data)
    except vol.Invalid:
        return
    for field, value in validated.items():
        if field in services.FIELD_VALIDATORS:
            assert services.FIELD_VALIDATORS[field](value) == value
            assert services.SERVICE_FIELDS[service].get(field) is not None, field
