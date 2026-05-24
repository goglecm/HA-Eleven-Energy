"""Constants for the Eleven Energy integration.

This module is the single source of truth for everything that needs to be
shared across the package - the integration ``DOMAIN`` slug, the list of
platforms Home Assistant forwards setup to, the API base URL, and the
poll-interval option name + bounds. Keeping these in one place avoids drift
between the config flow, options flow, controller, and number platform.
"""

from homeassistant.const import Platform

# Home Assistant integration domain. Must match the directory name under
# ``custom_components/`` and the ``domain`` field in ``manifest.json``.
DOMAIN = "eleven_energy"

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
BASE_URL = "https://portal.elevenenergy.co.uk/api/v1/"

# Key used in ``ConfigEntry.options`` to persist the user-chosen poll interval.
# The same key is used by the OptionsFlow schema, the Controller's
# ``poll_interval`` property, and the Number entity, so changing it would break
# the existing options stored on user installs.
CONF_POLL_INTERVAL = "poll_interval_seconds"

# Default cadence at which device data is fetched, in seconds. Picked to feel
# "live" without hammering the upstream API.
DEFAULT_POLL_INTERVAL_SECONDS = 15

# Inclusive bounds for the user-selectable poll interval. The lower bound
# protects the upstream API from accidental DoS via the Number entity; the
# upper bound keeps the inverter telemetry from drifting too far from real-time.
MIN_POLL_INTERVAL_SECONDS = 5
MAX_POLL_INTERVAL_SECONDS = 300

# Granularity of the OptionsFlow slider and Number entity. Five-second steps
# give a smooth slider while still landing on round numbers.
POLL_INTERVAL_STEP_SECONDS = 5
