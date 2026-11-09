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

from typing import Final

from homeassistant.const import Platform

try:
    # Home Assistant 2026.7 deprecated the bare ``PERCENTAGE`` constant as a
    # unit of measurement in favour of the ``UnitOfRatio`` enum. The enum does
    # not exist on the 2026.3 floor this integration still supports, so fall
    # back to the plain constant there. Both carry the value ``"%"``.
    from homeassistant.const import UnitOfRatio

    PERCENT: Final[str] = UnitOfRatio.PERCENTAGE
except ImportError:  # pragma: no cover - exercised on HA < 2026.7 only
    from homeassistant.const import PERCENTAGE as PERCENT  # type: ignore[assignment]

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

# Work-mode input limits. Service calls and device actions outside these
# ranges are rejected with a validation error before anything is sent to an
# inverter. ``MAX_POWER_KW`` matches the ceiling the Eleven Energy API itself
# accepts; the UI selectors stop at ``UI_MAX_POWER_KW``, which covers every
# current single-phase model, while YAML can go up to the API ceiling for
# larger machines.
MIN_POWER_KW: Final = 0.0
MAX_POWER_KW: Final = 50.0
UI_MAX_POWER_KW: Final = 20.0
MIN_TARGET_MINUTES: Final = 1
MAX_TARGET_MINUTES: Final = 1440

# Number of consecutive failed device polls after which every entity of that
# inverter is marked unavailable, so automations stop acting on stale values.
# At the default 15 s cadence this is roughly 45 s of outage.
UNAVAILABLE_AFTER_FAILED_POLLS: Final = 3

# Hard cap on auto-discovered (non-curated) entities per inverter. A payload
# that suddenly grows a dict keyed by timestamps would otherwise create new
# entities on every poll until the entity registry is unusable.
MAX_DYNAMIC_ENTITIES_PER_DEVICE: Final = 200
