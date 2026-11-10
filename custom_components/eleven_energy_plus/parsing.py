"""Strict parsing of values from the API, options and HTTP headers.

One parser per kind of value, shared by the whole integration, so the same
input is never accepted in one place and refused in another. Every parser
returns ``None`` for a value it cannot use and never guesses: ``True`` is not
1 kW, ``"maybe"`` is not ``False``, NaN is not a number, and ``"30.5"`` is
not a whole number of seconds. Callers decide how to report ``None``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import email.utils
import math
from typing import Any

from .const import MAX_RETRY_AFTER_SECONDS

# Strings accepted as booleans by :func:`parse_bool`.
_TRUE_STRINGS: frozenset[str] = frozenset({"true", "on", "yes", "1"})
_FALSE_STRINGS: frozenset[str] = frozenset({"false", "off", "no", "0"})


def finite_float(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None``.

    Numbers and numeric strings are accepted. Booleans are refused, and so
    are NaN and infinity, because ``min``/``max`` clamping silently turns NaN
    into a bound - "nan" kW would otherwise become full power.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def coerce_finite_number(value: Any) -> int | float | None:
    """Return ``value`` as a finite number for a sensor, or ``None``.

    Integers keep their type (so a count shows as ``4``, not ``4.0``);
    floats and numeric strings are parsed by :func:`finite_float`.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        return finite_float(value)
    return None


def parse_whole_number(value: Any) -> int | None:
    """Return ``value`` as an integer when it is a finite whole number.

    ``45``, ``45.0`` and ``"45"`` pass; ``45.5``, ``"30.5"``, ``True`` and
    NaN do not, so nothing is silently truncated.
    """
    number = finite_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def parse_bool(value: Any) -> bool | None:
    """Parse a boolean, or return ``None`` when the value is ambiguous.

    ``bool("false")`` is ``True``, which is exactly the wrong answer for a
    connectivity sensor or an "allow discharging" flag, so only real
    booleans, the numbers 0 and 1, and the usual words are accepted.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return None


def parse_retry_after(header: str | None, now: datetime) -> float | None:
    """Return the wait a ``Retry-After`` header asks for, in seconds.

    RFC 9110 allows either a number of seconds or an HTTP date; both are
    understood. The result is clamped to ``0..MAX_RETRY_AFTER_SECONDS`` (a
    date in the past means "now"). ``None`` means there is no usable hint.

    :param now: the current time, timezone-aware.
    """
    if header is None:
        return None
    text = header.strip()
    if not text:
        return None
    seconds = finite_float(text)
    if seconds is None:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when.tzinfo is None:
            # HTTP dates are always GMT; "-0000" parses as naive.
            when = when.replace(tzinfo=UTC)
        seconds = (when - now).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)
