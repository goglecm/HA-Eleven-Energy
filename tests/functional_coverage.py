"""Functional coverage model for Eleven Energy Plus.

Line and branch coverage say which code ran; this model says which promised
*behaviours* were checked. Every item is a behaviour the integration commits
to (README, CHANGELOG, code contracts), with a stable id. Tests declare what
they verify with ``@pytest.mark.covers("FC-...")`` (or on a parametrize case
with ``pytest.param(..., marks=pytest.mark.covers(...))``).

``tests/conftest.py`` collects the markers of *passing* tests, writes
``tests/functional_coverage_report.md`` and, on a full run, fails the session
when any item or cross-coverage cell is left uncovered, or when a marker
names an id that is not in this model.

Cross-coverage cells (``X-...``) are the combinations that must each be
exercised, not merely each dimension on its own.
"""

from __future__ import annotations

import itertools

ITEMS: dict[str, str] = {
    # --- Credentials and authentication ---------------------------------
    "FC-AUTH-01": "API-token entry sends the stored token as a Bearer credential",
    "FC-AUTH-02": "Sign-in entry sends the OAuth access token",
    "FC-AUTH-03": "A sign-in token refresh never reloads the entry",
    "FC-AUTH-04": "An expired sign-in token is refreshed before the request",
    "FC-AUTH-05": "A rejected credential at setup ends in SETUP_ERROR plus reauthentication",
    "FC-AUTH-06": "A rejected credential at runtime makes every entity unavailable and starts reauthentication",
    "FC-AUTH-07": "A refused sign-in refresh is definitive (reauthentication, no recovery probe)",
    "FC-AUTH-08": "A transient rejection recovers on its own through the recovery probe",
    "FC-AUTH-09": "A blank stored token starts reauthentication",
    # --- Config, reconfigure, reauth flows --------------------------------
    "FC-FLOW-01": "Setup offers a menu: sign in, or API token",
    "FC-FLOW-02": "The token step validates the token against the API before saving",
    "FC-FLOW-03": "Sign-in uses PKCE, the full scope set and the my.home-assistant.io redirect",
    "FC-FLOW-04": "Only one entry can be configured",
    "FC-FLOW-05": "Reconfigure switches an entry to an API token",
    "FC-FLOW-06": "Reconfigure switches an entry to an account sign-in",
    "FC-FLOW-07": "Reauthentication of an API-token entry asks for a new token",
    "FC-FLOW-08": "Reauthentication of a sign-in entry signs in again",
    "FC-FLOW-09": "A credential for a different site is refused on reauth and reconfigure",
    "FC-FLOW-10": "A replaced credential replaces the entry data and reloads exactly once",
    "FC-FLOW-11": "Each authorization uses a fresh PKCE verifier whose challenge matches",
    "FC-FLOW-12": "A site without hybrid inverters is refused when connecting",
    # --- Options -------------------------------------------------------------
    "FC-OPT-01": "Options show only the update interval and the device-label override",
    "FC-OPT-02": "An interval change applies live without a reload",
    "FC-OPT-03": "A device-label override change reloads and relabels the device",
    "FC-OPT-04": "An invalid device-label override is refused with an error",
    "FC-OPT-05": "The Update Interval number entity sets the interval",
    "FC-OPT-06": "A stored interval is parsed strictly (booleans, fractions, text, range)",
    # --- Discovery -------------------------------------------------------------
    "FC-DISC-01": "Hybrid inverters are discovered and other device types ignored",
    "FC-DISC-02": "Each new inverter is read once before its entities are built",
    "FC-DISC-03": "Device label precedence: override > model.name > name > fallbacks",
    "FC-DISC-04": "An inverter added at runtime gets sensors, binary sensors and an Update Interval entity",
    "FC-DISC-05": "An inverter that disappears from the site becomes unavailable, raises an issue and can be removed",
    "FC-DISC-06": "Malformed site payloads are reported and retried, never crash",
    "FC-DISC-07": "Discovery survives a failed device read",
    # --- Entities -------------------------------------------------------------
    "FC-ENT-01": "Curated entities have the documented units, device classes and categories",
    "FC-ENT-02": "Three-phase models get per-phase entities; single-phase models do not",
    "FC-ENT-03": "The number of MPPT entity sets comes from the model",
    "FC-ENT-04": "Battery entity sets follow the reading, including batteries fitted later",
    "FC-ENT-05": "Nameplate ratings come from the model block",
    "FC-ENT-06": "Unknown fields are auto-discovered with inferred units",
    "FC-ENT-07": "Auto-discovery is capped per inverter",
    "FC-ENT-08": "Generic lists are expanded and truncated at the cap",
    "FC-ENT-09": "Entity ids are always in the platform's own domain",
    "FC-ENT-10": "The API Connection sensor is always available and reflects polling",
    # --- Values ---------------------------------------------------------------
    "FC-VAL-01": "Numeric sensors only report finite numbers",
    "FC-VAL-02": "Binary sensors parse strictly and start unknown",
    "FC-VAL-03": "A null or missing field reads unknown, never the previous value",
    "FC-VAL-04": "A one-off dip on a total_increasing counter is not reported as a reset",
    "FC-VAL-05": "A negative total_increasing value is rejected",
    "FC-VAL-06": "Indexed lists are keyed by own index; out-of-set indices are reported",
    "FC-VAL-07": "Translated state values are normalised to their translation keys",
    # --- Availability ----------------------------------------------------------
    "FC-AVAIL-01": "Entities go unavailable after consecutive failed polls and recover",
    "FC-AVAIL-02": "A reading that cannot be processed counts as a failed poll",
    "FC-AVAIL-03": "An offline inverter's telemetry is unavailable; exempt entities stay available",
    "FC-AVAIL-04": "Stopping the controller marks every entity unavailable",
    "FC-AVAIL-05": "The Update Interval entity stays available during outages",
    # --- Actions (services) ----------------------------------------------------
    "FC-SVC-01": "Every numeric action field rejects NaN, infinity, booleans, text and out-of-range values",
    "FC-SVC-02": "Every boolean action field rejects ambiguous values",
    "FC-SVC-03": "Unknown action fields are rejected",
    "FC-SVC-04": "Missing required action fields are rejected",
    "FC-SVC-05": "percentage_to_battery is accepted as an alias",
    "FC-SVC-06": "A successful action returns the response when asked and nothing otherwise",
    "FC-SVC-07": "A failed action raises without a response request and returns the result with one",
    "FC-SVC-08": "Each failure kind maps to its exception class and translation key",
    "FC-SVC-09": "The inverter is resolved from device_id, entity_id or area_id",
    "FC-SVC-10": "Untargeted, ambiguous and unmatched targets are refused on multi-inverter sites",
    "FC-SVC-11": "The actions survive reloads and unloads",
    "FC-SVC-12": "Calling an action with no loaded entry reports it",
    "FC-SVC-13": "An unexpected internal error is contained, reported and raised as a translated error",
    # --- Command delivery -----------------------------------------------------
    "FC-CMD-01": "Only network errors, timeouts, 408, 429 and 5xx are retried",
    "FC-CMD-02": "Retry-After is honoured as seconds or as an HTTP date",
    "FC-CMD-03": "The newest command to an inverter wins; older ones are superseded",
    "FC-CMD-04": "Commands to different inverters do not block each other",
    "FC-CMD-05": "Shutdown aborts a command's retry backoff",
    "FC-CMD-06": "A successful command triggers an early poll",
    # --- Polling --------------------------------------------------------------
    "FC-POLL-01": "The poller polls immediately, then at the configured interval",
    "FC-POLL-02": "Rate limiting (429) defers the next poll",
    "FC-POLL-03": "An early-poll request never shortens a command's backoff",
    # --- Device actions --------------------------------------------------------
    "FC-DEVACT-01": "Device actions are offered for Eleven Energy Plus devices only",
    "FC-DEVACT-02": "Each device action offers exactly its work mode's fields",
    "FC-DEVACT-03": "Device action configs are validated when the automation loads",
    "FC-DEVACT-04": "A device action delegates to its action and raises on failure",
    # --- Notifications and repairs -------------------------------------------
    "FC-NOTIF-01": "cloud_unreachable is raised after 15 minutes of failed polls and cleared on recovery",
    "FC-NOTIF-02": "inverter_offline is raised after 15 minutes offline and cleared when online",
    "FC-NOTIF-03": "inverter_missing is raised and cleared with the site listing",
    "FC-NOTIF-04": "work_mode_failed is raised for API, auth and internal failures (not bad input) and cleared on success",
    "FC-NOTIF-05": "optimiser_off is raised 24 hours after a non-reset work mode and cleared by Reset",
    "FC-NOTIF-06": "The optimiser timer survives a restart",
    "FC-NOTIF-07": "A wall clock moving backwards restarts the optimiser timer",
    "FC-NOTIF-08": "Each issue creates one persistent notification, dismissed when it clears",
    "FC-NOTIF-09": "A rejected credential creates a notification, dismissed on recovery",
    "FC-NOTIF-10": "Issues, notifications and stored state are removed with the entry",
    # --- Migration -------------------------------------------------------------
    "FC-MIG-01": "Positional entities migrate to index-keyed ones keeping their entity id",
    "FC-MIG-02": "Obsolete entities are removed only when the hardware is known",
    "FC-MIG-03": "The migration is idempotent",
    # --- Diagnostics ----------------------------------------------------------
    "FC-DIAG-01": "Diagnostics have the documented shape and read the version without file I/O",
    "FC-DIAG-02": "Credentials, serials, ids and personal fields are redacted",
    "FC-DIAG-03": "Diagnostics include per-inverter state, active issues and the last successful poll",
    "FC-DIAG-04": "Diagnostics work without a loaded controller",
    # --- Date and time ---------------------------------------------------------
    "FC-TIME-01": "Durations use the monotonic clock and ignore wall-clock jumps",
    "FC-TIME-02": "A counter reset at local midnight is accepted in any timezone",
    "FC-TIME-03": "DST transitions do not disturb thresholds or the counter debounce",
    "FC-TIME-04": "Retry-After HTTP dates in the past, future or malformed are handled",
    "FC-TIME-05": "A sign-in token expiring mid-session is refreshed transparently",
    # --- Lifecycle --------------------------------------------------------------
    "FC-LIFE-01": "Setup and unload complete cleanly",
    "FC-LIFE-02": "Setup retries when the API is unreachable, lists no inverter or answers malformed",
    "FC-LIFE-03": "A failure while forwarding platforms rolls the controller back",
    "FC-LIFE-04": "A reload keeps the actions, leaves no stale state and stops the old poller",
    "FC-LIFE-05": "Removing the entry cleans up everything it created",
    "FC-LIFE-06": "YAML configuration is not supported",
    # --- Static contracts ------------------------------------------------------
    "FC-STATIC-01": "services.yaml satisfies hassfest's current rules",
    "FC-STATIC-02": "Translations are complete and contain no unused keys",
    "FC-STATIC-03": "Every Home Assistant import is public on the installed version",
    "FC-STATIC-04": "manifest.json and hacs.json carry the required fields",
    "FC-STATIC-05": "No except block swallows an error silently",
    "FC-STATIC-06": "README behavioural statements map to model items",
}

# --- Cross-coverage dimensions --------------------------------------------

CREDENTIAL_KINDS = ("token", "signin")
FLOW_SOURCES = ("user", "reauth", "reconfigure")
CREDENTIAL_OUTCOMES = (
    "valid",
    "invalid_auth",
    "cannot_connect",
    "no_inverters",
    "different_site",
)

SERVICES = (
    "set_work_mode_self_consumption",
    "set_work_mode_force_charge",
    "set_work_mode_grid_export",
    "set_work_mode_pv_export",
    "set_work_mode_idle_battery",
    "set_work_mode_target_soc",
    "set_work_mode_reset",
)
RESPONSE_MODES = ("response", "no_response")
SERVICE_OUTCOMES = (
    "success",
    "validation",
    "target",
    "auth",
    "api",
    "superseded",
    "shutdown",
    "internal",
    "not_loaded",
)

ENTITY_KINDS = ("sensor", "binary_sensor", "number", "api_connection")
UNAVAILABILITY_CAUSES = ("poll_failures", "auth", "offline", "missing", "shutdown")

NUMERIC_FIELDS = (
    "percent_to_battery",
    "target_power",
    "target_percent",
    "target_soc",
    "target_minutes",
    "max_charge_power",
    "max_discharge_power",
    "poll_interval_option",
)
INVALID_NUMBER_KINDS = ("nan", "inf", "bool", "text", "out_of_range")


def _cross_cells() -> dict[str, str]:
    cells: dict[str, str] = {}
    for kind, source, outcome in itertools.product(
        CREDENTIAL_KINDS, FLOW_SOURCES, CREDENTIAL_OUTCOMES
    ):
        if source == "user" and outcome == "different_site":
            continue  # nothing to compare against on first setup
        cells[f"X-CRED-{kind}-{source}-{outcome}"] = (
            f"{kind} credential via {source}: {outcome}"
        )
    for service, mode, outcome in itertools.product(
        SERVICES, RESPONSE_MODES, SERVICE_OUTCOMES
    ):
        cells[f"X-SVC-{service}-{mode}-{outcome}"] = (
            f"{service} ({mode}): {outcome}"
        )
    for kind, cause in itertools.product(ENTITY_KINDS, UNAVAILABILITY_CAUSES):
        cells[f"X-AVAIL-{kind}-{cause}"] = f"{kind} under {cause}"
    for field, invalid in itertools.product(NUMERIC_FIELDS, INVALID_NUMBER_KINDS):
        cells[f"X-NUM-{field}-{invalid}"] = f"{field} given {invalid}"
    return cells


CROSS_CELLS: dict[str, str] = _cross_cells()

ALL_IDS: dict[str, str] = {**ITEMS, **CROSS_CELLS}
