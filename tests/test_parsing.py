"""Tests for parsing.py: the strict parsers shared by the whole integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import email.utils
import math
from typing import Any

from hypothesis import given, strategies as st
import pytest

from custom_components.eleven_energy_plus.const import MAX_RETRY_AFTER_SECONDS
from custom_components.eleven_energy_plus.parsing import (
    coerce_finite_number,
    finite_float,
    parse_bool,
    parse_retry_after,
    parse_whole_number,
)

_NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


class TestFiniteFloat:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(1, 1.0), (1.5, 1.5), ("2.25", 2.25), (" 3 ", 3.0), (0, 0.0), (-4, -4.0), (Decimal("1.5"), 1.5)],
    )
    def test_accepts(self, value: Any, expected: float) -> None:
        assert finite_float(value) == expected

    @pytest.mark.parametrize(
        "value", [True, False, "N/A", "", math.nan, math.inf, -math.inf, "nan", "inf", None, {}, [], 10**400]
    )
    def test_refuses(self, value: Any) -> None:
        assert finite_float(value) is None


class TestCoerceFiniteNumber:
    def test_keeps_integers(self) -> None:
        assert coerce_finite_number(4) == 4
        assert isinstance(coerce_finite_number(4), int)

    @pytest.mark.parametrize(("value", "expected"), [(1.5, 1.5), ("2.25", 2.25), (" 3 ", 3.0)])
    def test_parses_floats_and_strings(self, value: Any, expected: float) -> None:
        assert coerce_finite_number(value) == expected

    @pytest.mark.parametrize("value", [True, False, "N/A", math.nan, math.inf, "nan", None, {}, [], Decimal(1)])
    def test_refuses(self, value: Any) -> None:
        assert coerce_finite_number(value) is None


class TestParseWholeNumber:
    @pytest.mark.parametrize(("value", "expected"), [(45, 45), (45.0, 45), ("45", 45), (" 7 ", 7), (-5, -5)])
    def test_accepts(self, value: Any, expected: int) -> None:
        assert parse_whole_number(value) == expected

    @pytest.mark.parametrize("value", [45.5, "30.5", True, False, math.nan, "fast", None, math.inf])
    def test_refuses_without_truncating(self, value: Any) -> None:
        assert parse_whole_number(value) is None


class TestParseBool:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("true", True),
            ("False", False),
            (" ON ", True),
            ("no", False),
            ("Yes", True),
            ("off", False),
            ("1", True),
            ("0", False),
            (1, True),
            (0, False),
            (1.0, True),
            (0.0, False),
        ],
    )
    def test_unambiguous_values(self, value: Any, expected: bool) -> None:
        assert parse_bool(value) is expected

    @pytest.mark.parametrize("value", [2, -1, 0.5, math.nan, "maybe", "", "2", None, [], {}, "yesno"])
    def test_ambiguous_values_are_none(self, value: Any) -> None:
        """Regression: ``bool("false")`` used to turn the Online sensor on."""
        assert parse_bool(value) is None


def _http_date(when: datetime) -> str:
    return email.utils.format_datetime(when, usegmt=True)


@pytest.mark.covers("FC-TIME-04")
class TestParseRetryAfter:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("7", 7.0),
            (" 7 ", 7.0),
            ("0", 0.0),
            ("-5", 0.0),
            ("1e3", MAX_RETRY_AFTER_SECONDS),
            ("500", MAX_RETRY_AFTER_SECONDS),
            (_http_date(_NOW + timedelta(seconds=7)), 7.0),
            (_http_date(_NOW - timedelta(hours=1)), 0.0),
            (_http_date(_NOW + timedelta(days=3)), MAX_RETRY_AFTER_SECONDS),
            ("Mon, 09 Nov 2026 12:00:30 -0000", 30.0),
        ],
    )
    def test_seconds_and_dates(self, header: str, expected: float) -> None:
        assert parse_retry_after(header, _NOW) == pytest.approx(expected)

    @pytest.mark.parametrize("header", [None, "", "   ", "soon-ish", "nan", "inf", "Mon, 99 Foo 2026"])
    def test_unusable_hints(self, header: str | None) -> None:
        assert parse_retry_after(header, _NOW) is None


@pytest.mark.covers("FC-TIME-04")
@given(st.text(max_size=40))
def test_any_retry_after_header_yields_a_bounded_wait_or_none(header: str) -> None:
    wait = parse_retry_after(header, _NOW)
    assert wait is None or 0.0 <= wait <= MAX_RETRY_AFTER_SECONDS


# hypothesis takes naive bounds and applies ``timezones`` itself.
@pytest.mark.covers("FC-TIME-04")
@given(
    st.datetimes(
        timezones=st.just(UTC),
        min_value=datetime(1900, 1, 1),  # noqa: DTZ001
        max_value=datetime(2200, 1, 1),  # noqa: DTZ001
    )
)
def test_any_retry_after_http_date_yields_a_bounded_wait(when: datetime) -> None:
    wait = parse_retry_after(_http_date(when), _NOW)
    assert wait is not None
    assert 0.0 <= wait <= MAX_RETRY_AFTER_SECONDS
