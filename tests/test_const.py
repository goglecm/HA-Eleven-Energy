"""Tests for const.py: the shared limits agree with each other."""

from __future__ import annotations

from datetime import timedelta

from custom_components.eleven_energy_plus import const


def test_poll_interval_bounds_are_consistent() -> None:
    assert const.MIN_POLL_INTERVAL_SECONDS < const.DEFAULT_POLL_INTERVAL_SECONDS < const.MAX_POLL_INTERVAL_SECONDS
    for value in (
        const.MIN_POLL_INTERVAL_SECONDS,
        const.DEFAULT_POLL_INTERVAL_SECONDS,
        const.MAX_POLL_INTERVAL_SECONDS,
    ):
        assert value % const.POLL_INTERVAL_STEP_SECONDS == 0


def test_power_and_minute_limits_are_consistent() -> None:
    assert const.MIN_POWER_KW == 0.0
    assert const.MIN_POWER_KW < const.UI_MAX_POWER_KW <= const.MAX_POWER_KW
    assert 1 <= const.MIN_TARGET_MINUTES < const.MAX_TARGET_MINUTES == 24 * 60


def test_fault_thresholds_are_sane() -> None:
    assert const.UNAVAILABLE_AFTER_FAILED_POLLS >= 2
    assert const.MISSING_AFTER_SITE_POLLS >= 2, "one truncated listing must not hide an inverter"
    assert const.CLOUD_UNREACHABLE_AFTER_SECONDS == const.INVERTER_OFFLINE_AFTER_SECONDS == 900
    assert const.OPTIMISER_OFF_AFTER.total_seconds() == timedelta(hours=24).total_seconds()
    assert const.AUTH_RECOVERY_SECONDS == 600
    assert 0 < const.RATE_LIMIT_BACKOFF_SECONDS <= const.MAX_RETRY_AFTER_SECONDS


def test_unit_and_domain() -> None:
    assert const.PERCENT == "%"
    assert const.DOMAIN == "eleven_energy_plus"
    assert const.BASE_URL.endswith("/")
    assert {platform.value for platform in const.PLATFORMS} == {"binary_sensor", "number", "sensor"}


def test_error_kinds_are_distinct_strings() -> None:
    values = [kind.value for kind in const.ErrorKind]
    assert len(values) == len(set(values))
    assert all(isinstance(kind, str) for kind in const.ErrorKind)
