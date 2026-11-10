"""Constants for the Eleven Energy Plus integration.

This module is the single source of truth for everything that needs to be
shared across the package - the integration ``DOMAIN`` slug, the list of
platforms Home Assistant forwards setup to, the API and OAuth endpoints, the
poll-interval option and its bounds, the work-mode input limits, and the
availability / auto-discovery safety thresholds. Keeping these in one place
avoids drift between the config flow, options flow, service schemas, device
actions, controller and entity platforms.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

from homeassistant.const import Platform

# The percent unit. Home Assistant 2026.7 deprecated the name of the bare
# ``PERCENTAGE`` constant in favour of ``UnitOfRatio.PERCENTAGE``, which does
# not exist on the 2026.3 floor. Both are the string ``"%"`` (the enum is a
# StrEnum), and units are compared by value, so the string itself is the same
# unit on every supported version without importing either name.
PERCENT: Final = "%"

# Home Assistant integration domain. Must match the directory name under
# ``custom_components/`` and the ``domain`` field in ``manifest.json``.
#
# This fork uses ``eleven_energy_plus`` rather than ``eleven_energy`` so it can
# be installed side-by-side with the original ``iPeel/HA-Eleven-Energy``
# integration without colliding on filesystem path, HA entity registry
# namespace, or service prefixes.
DOMAIN: Final = "eleven_energy_plus"

# Platforms Home Assistant forwards ``async_setup_entry`` to. The order is
# significant only insofar as ``Controller.complete_platform_setup`` waits for
# every platform here to register before starting the background poller.
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SENSOR,
]

# Eleven Energy cloud API root. All controller and config-flow HTTP calls are
# rooted here; trailing slash is intentional so callers can ``f"{BASE_URL}site"``.
BASE_URL: Final = "https://portal.elevenenergy.co.uk/api/v1/"

# Config entry data keys. An API-key entry stores the key as a string under
# ``token``. An account sign-in (OAuth) entry stores Home Assistant's token
# structure under the same ``token`` key and additionally carries
# ``auth_implementation``; the presence of that key is what tells them apart.
CONF_TOKEN: Final = "token"
CONF_AUTH_IMPLEMENTATION: Final = "auth_implementation"

# Account sign-in. The client id is deliberately not a secret: it is a public
# OAuth client registered by Eleven Energy for Home Assistant, it has no secret
# at all, and the authorization server requires PKCE from it instead. The
# server only has the my.home-assistant.io redirect registered for it.
OAUTH_CLIENT_ID: Final = "home-assistant"
OAUTH_AUTHORIZE_URL: Final = "https://integrate.elevenenergy.co.uk/oauth2/authorize"
OAUTH_TOKEN_URL: Final = "https://integrate.elevenenergy.co.uk/oauth2/token"
OAUTH_SCOPES: Final[tuple[str, ...]] = (
    "site:read",
    "devices:read",
    "telemetry:read",
    "control:write",
)

# Key used in ``ConfigEntry.options`` to persist the user-chosen poll interval.
# The same key is used by the OptionsFlow schema, the Controller's
# ``poll_interval`` property, and the Number entity, so changing it would break
# the existing options stored on user installs.
CONF_POLL_INTERVAL: Final = "poll_interval_seconds"

# Default cadence at which device data is fetched, in seconds. Picked to feel
# "live" without hammering the upstream API.
DEFAULT_POLL_INTERVAL_SECONDS: Final = 15

# Inclusive bounds for the user-selectable poll interval. The lower bound
# protects the upstream API from accidental DoS via the Number entity; the
# upper bound keeps the inverter telemetry from drifting too far from real-time.
MIN_POLL_INTERVAL_SECONDS: Final = 5
MAX_POLL_INTERVAL_SECONDS: Final = 300

# Granularity of the OptionsFlow slider and Number entity. Five-second steps
# give a smooth slider while still landing on round numbers.
POLL_INTERVAL_STEP_SECONDS: Final = 5

# Key used in ``ConfigEntry.options`` to persist an optional override for the
# device-card label / model. Empty string or missing key means "no override -
# use the API-derived label" (the ``model`` block's ``name``, falling back to
# the device ``name`` field).
CONF_DEVICE_LABEL: Final = "device_label_override"

# Longest accepted device-label override. Longer text, or text containing
# control characters (newlines included), is refused by the options form.
MAX_DEVICE_LABEL_LENGTH: Final = 64

# Work-mode input limits. Service calls and device actions outside these
# ranges are rejected with a validation error before anything is sent to an
# inverter. ``MAX_POWER_KW`` is the ceiling the integration accepts for any
# machine. The device-action form's power selector stops at
# ``UI_MAX_POWER_KW`` so the field stays usable; YAML and scripts can go up
# to ``MAX_POWER_KW``.
MIN_POWER_KW: Final = 0.0
MAX_POWER_KW: Final = 50.0
UI_MAX_POWER_KW: Final = 20.0
MIN_TARGET_MINUTES: Final = 1
MAX_TARGET_MINUTES: Final = 1440

# Number of consecutive failed device polls after which every entity of that
# inverter is marked unavailable, so automations stop acting on stale values.
# At the default 15 s cadence this is roughly 45 s of outage.
UNAVAILABLE_AFTER_FAILED_POLLS: Final = 3

# An inverter that is missing from this many consecutive successful site
# listings is treated as removed from the site: its entities go unavailable
# and a repair issue explains that the device can be deleted. One listing is
# not enough, so a single truncated response cannot hide a machine.
MISSING_AFTER_SITE_POLLS: Final = 2

# While the API rejects the credential, an API-token entry re-tests it this
# often (seconds), so a rejection caused by a transient fault on Eleven
# Energy's side recovers on its own. A refused account sign-in refresh is
# definitive and is not re-tested.
AUTH_RECOVERY_SECONDS: Final = 600

# ``Retry-After`` hints (seconds or an HTTP date) are clamped to this range,
# so a hostile or broken header can neither stall polling for days nor turn
# into a busy loop.
MAX_RETRY_AFTER_SECONDS: Final = 300.0

# How long polling backs off after HTTP 429 without a usable ``Retry-After``.
RATE_LIMIT_BACKOFF_SECONDS: Final = 60.0

# Thresholds after which a lasting fault becomes a repair issue (and, the
# first time, a persistent notification). Durations are measured with the
# monotonic clock, so wall-clock changes cannot trigger or suppress them.
CLOUD_UNREACHABLE_AFTER_SECONDS: Final = 15 * 60
INVERTER_OFFLINE_AFTER_SECONDS: Final = 15 * 60

# Setting any work mode switches Eleven Energy's optimiser off until Reset
# Work Mode is sent. After this long without a reset, a repair issue says so.
# Measured in UTC wall-clock time and persisted, because it must survive a
# restart; a clock that moves backwards restarts the measurement.
OPTIMISER_OFF_AFTER: Final = timedelta(hours=24)

# Hard cap on auto-discovered (non-curated) entities per inverter. A payload
# that suddenly grows a dict keyed by timestamps would otherwise create new
# entities on every poll until the entity registry is unusable.
MAX_DYNAMIC_ENTITIES_PER_DEVICE: Final = 200


class ErrorKind(StrEnum):
    """Why a work-mode action failed; reported as ``error_kind``.

    Each kind maps to exactly one exception class and translation key in
    :mod:`.services` for callers that did not ask for a response.
    """

    INVALID_REQUEST = "invalid_request"
    CREDENTIAL_REJECTED = "credential_rejected"
    API_ERROR = "api_error"
    SUPERSEDED = "superseded"
    ABORTED = "aborted"
    INTERNAL_ERROR = "internal_error"
    NOT_LOADED = "not_loaded"
