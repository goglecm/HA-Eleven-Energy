"""The main Eleven Energy Plus coordinator.

The :class:`Controller` is the only thing in the integration that talks to
the Eleven Energy cloud API. It owns:

* The credential - either an API key, or an account sign-in whose access
  token Home Assistant refreshes through an ``OAuth2Session`` - and the
  shared aiohttp session via :func:`async_get_clientsession`. Headers are
  built per request so a refreshed access token is always the one used.
* The discovered :class:`HybridInverter` devices and their entities.
* The background polling task that periodically refreshes site + device
  data, feeds it through each inverter's payload walker, and drives entity
  availability.
* The dispatch path for the seven ``set_work_mode_*`` service calls: target
  resolution, required-field and value validation, per-inverter
  serialisation (a newer command supersedes an older one still retrying),
  a bounded retry policy, and a stable structured response.
* The :class:`~.notifications.FaultReporter` that turns lasting faults into
  repair issues and notifications.
* A small listener pub-sub used by the Number entity so it can rebroadcast
  cadence changes triggered via the OptionsFlow, and go unavailable on
  shutdown.

Failure handling, in one place:

* The API rejecting the credential (HTTP 401/403, or an account sign-in
  whose refresh is refused) raises ``ConfigEntryAuthFailed`` during setup
  and starts Home Assistant's reauthentication flow at runtime, with a
  persistent notification. Every inverter goes unavailable at once and
  polling pauses. A rejection by HTTP status is re-tested every
  :data:`~.const.AUTH_RECOVERY_SECONDS`: when the API accepts the
  credential again, the reauthentication prompt is withdrawn and polling
  resumes. A refused sign-in refresh is definitive.
* Anything that merely stops us reaching the API raises
  ``ConfigEntryNotReady`` during setup, so Home Assistant retries on its
  own schedule. At runtime, a device goes unavailable after
  :data:`~.const.UNAVAILABLE_AFTER_FAILED_POLLS` consecutive failed polls -
  including a reading the integration fails to process - and a repair
  issue appears once every poll has failed for
  :data:`~.const.CLOUD_UNREACHABLE_AFTER_SECONDS`.
* HTTP 429 defers the next poll (or POST retry) by its ``Retry-After``,
  given as seconds or as an HTTP date.
* An inverter missing from :data:`~.const.MISSING_AFTER_SITE_POLLS`
  consecutive site listings goes unavailable, with a repair issue; its
  device can then be deleted.
* Device ids from the listing go into request paths encoded as a single
  path segment (:func:`device_path`), so no id can address another device.

Everything here is async and cancellation-safe: the poller waits on a
``poll now`` event (set by option changes and successful commands), and
both the poller and the POST backoff wake immediately on the separate
``shutdown`` event that :meth:`terminate` sets. A command can outlive its
controller (an unload or reload while it is being sent); its outcome is then
reported to the controller running the entry, if any.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import contextlib
from dataclasses import dataclass, field
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    AUTH_RECOVERY_SECONDS,
    BASE_URL,
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MAX_POWER_KW,
    MAX_TARGET_MINUTES,
    MIN_POLL_INTERVAL_SECONDS,
    MIN_POWER_KW,
    MIN_TARGET_MINUTES,
    MISSING_AFTER_SITE_POLLS,
    PLATFORMS,
    RATE_LIMIT_BACKOFF_SECONDS,
    ErrorKind,
)
from .hybrid_inverter import HybridInverter, device_card_name
from .notifications import FaultReporter
from .parsing import finite_float, parse_bool, parse_retry_after, parse_whole_number

if TYPE_CHECKING:
    from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

_LOGGER = logging.getLogger(__name__)


# All outbound API calls share this timeout. ``total`` bounds the whole request
# including the response body; ``connect`` bounds the TCP/TLS handshake. Without
# these, a hung upstream would freeze the polling loop indefinitely.
API_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

# Caps on POST retries. With a doubling delay starting at 1s and a 5-attempt
# cap the worst case wait is 1+2+4+8 = 15s of sleeping plus 5 * total request
# timeout, which is bounded and cancellable via the shutdown event. A
# ``Retry-After`` header may lengthen a single wait, up to the backoff cap.
_MAX_POST_ATTEMPTS = 5
_MAX_BACKOFF_SECONDS = 30.0

# Maximum length of the debug dump of the first device payload. Auto-discovered
# list fields can balloon the size of this log line.
_PAYLOAD_LOG_LIMIT = 2000

# HTTP statuses worth retrying: request timeout, rate limiting and server-side
# errors. Every other 4xx means the request itself is wrong and will fail the
# same way again; 401/403 additionally start reauthentication.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429})


# Required fields per work mode service. Anything declared required here is
# treated as a hard precondition; missing values short-circuit the service call
# with an error response rather than silently sending a partial command.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "set_work_mode_self_consumption": (),
    "set_work_mode_force_charge": ("target_power", "target_percent"),
    "set_work_mode_grid_export": ("target_power", "target_percent"),
    "set_work_mode_pv_export": (),
    "set_work_mode_idle_battery": (),
    "set_work_mode_target_soc": ("target_soc", "target_minutes"),
    "set_work_mode_reset": (),
}


# Canonical API names per service. Used both for the POST body and as the
# ``work_mode`` field in the response dict.
_API_WORK_MODE_NAMES: dict[str, str] = {
    "set_work_mode_self_consumption": "selfConsumption",
    "set_work_mode_force_charge": "forceCharge",
    "set_work_mode_grid_export": "gridExport",
    "set_work_mode_pv_export": "pvExportPriority",
    "set_work_mode_idle_battery": "idleBattery",
    "set_work_mode_target_soc": "targetSoc",
    "set_work_mode_reset": "reset",
}

# What a ``work_mode_failed`` issue is raised for when the command failed
# before an inverter was resolved.
_SITE_ISSUE_TARGET = "site"

# Failure kinds that leave a ``work_mode_failed`` repair issue. Invalid input
# is the caller's to fix and is raised to the caller; a superseded or aborted
# command was replaced or stopped on purpose.
_ISSUE_KINDS: frozenset[ErrorKind] = frozenset(
    {ErrorKind.API_ERROR, ErrorKind.CREDENTIAL_REJECTED, ErrorKind.INTERNAL_ERROR}
)


def _valid_percent(value: Any) -> float | None:
    """Return a finite percentage in 0-100, or None."""
    number = finite_float(value)
    if number is None or not 0.0 <= number <= 100.0:
        return None
    return number


def _valid_power_kw(value: Any) -> float | None:
    """Return a finite power within the accepted kW range, or None."""
    number = finite_float(value)
    if number is None or not MIN_POWER_KW <= number <= MAX_POWER_KW:
        return None
    return number


def _valid_minutes(value: Any) -> int | None:
    """Return a whole number of minutes within the accepted range, or None."""
    minutes = parse_whole_number(value)
    if minutes is None or not MIN_TARGET_MINUTES <= minutes <= MAX_TARGET_MINUTES:
        return None
    return minutes


def _as_list(value: Any) -> list[str]:
    """Normalise a service-call target value (str or list) to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def model_block(summary: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    """Return the API's ``model`` block from the site listing or the device reading."""
    for source in (summary, state or {}):
        model = source.get("model")
        if isinstance(model, dict) and model:
            return model
    return {}


def api_device_label(summary: dict[str, Any], state: dict[str, Any] | None) -> str:
    """Pick the product label the API reports for a device.

    The ``model`` block's ``name`` is the real product name. The device's own
    ``name`` field is a provisioning default that was never corrected per
    model - every machine in the fleet carries the same one (the reason a
    Mediterranean Sea 12 could show up as "North Sea 6") - so it is only a
    fallback for a portal too old to send the model block. ``model`` as a
    plain string, ``productName`` and ``inverterModel`` are older fallbacks,
    and ``"Inverter"`` keeps the device card reading "Eleven Energy Plus
    Inverter" rather than nothing at all.
    """
    name = model_block(summary, state).get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    for key in ("name", "model", "productName", "inverterModel"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Inverter"


# Ids that no encoding can make a path segment of their own: in a URL they
# mean "this directory" and "the parent directory".
_DOT_SEGMENTS = frozenset({".", ".."})
_refused_ids: set[str] = set()


def listed_inverter_ids(payload: dict[str, Any]) -> list[str]:
    """Return the device ids of the hybrid inverters in a ``/site`` payload.

    An id that cannot address a device of its own (``.`` or ``..``) is left
    out, with a warning the first time it is seen.
    """
    ids: list[str] = []
    for summary in payload.get("devices", []):
        if not isinstance(summary, dict):
            continue
        device_id = summary.get("deviceId")
        if (
            not isinstance(device_id, str)
            or not device_id
            or summary.get("type") != "hybridinverter"
            or device_id in ids
        ):
            continue
        if device_id in _DOT_SEGMENTS:
            if device_id not in _refused_ids:
                _refused_ids.add(device_id)
                _LOGGER.warning(
                    "Eleven Energy Plus ignored an inverter listed with the "
                    "unusable device id %r",
                    device_id,
                )
            continue
        ids.append(device_id)
    return ids


def device_path(device_id: str, suffix: str = "") -> str:
    """Return the API path of a device, its id encoded as one path segment.

    Unencoded, an id such as ``x/../b`` would address device ``b``.
    """
    return f"devices/{quote(device_id, safe='')}{suffix}"


class ElevenEnergyAuthError(Exception):
    """The API rejected the credential, or it could not be refreshed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Store the HTTP status that caused the failure, if there was one."""
        super().__init__(message)
        self.status = status


class ElevenEnergyApiError(Exception):
    """The API could not be reached or returned something unusable."""


class ElevenEnergyRateLimitedError(ElevenEnergyApiError):
    """The API answered HTTP 429; ``retry_after`` is its hint in seconds."""

    def __init__(self, message: str, retry_after: float | None) -> None:
        """Store the parsed ``Retry-After`` hint (None when there was none)."""
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class PostResult:
    """Outcome of a ``send_reliable_post`` call.

    ``kind`` is None on success, else why it failed: the credential was
    rejected, the API failed, a newer command superseded it, or shutdown
    aborted it.
    """

    status: int | None
    body: Any | None
    attempts: int
    error: str | None = None
    kind: ErrorKind | None = None


@dataclass
class _WorkModeRequest:
    """Sanitised user-facing parameters plus the API payload they translate to."""

    user_params: dict[str, Any] = field(default_factory=dict)
    api_params: dict[str, Any] = field(default_factory=dict)
    invalid: list[str] = field(default_factory=list)


class Controller:
    """Coordinator for a single Eleven Energy Plus config entry.

    A controller is constructed by :func:`async_setup_entry`, populated by
    :meth:`initialise` (which discovers the hybrid inverters and reads each
    once), and then handed off to the platform setups. As each platform
    registers its ``async_add_entities`` callback via
    :meth:`complete_platform_setup`, the controller waits until all platforms
    in ``PLATFORMS`` have checked in before starting :meth:`start_poller`.

    Lifecycle methods (in call order):

    * :meth:`initialise` - one-shot discovery; raises ``ConfigEntryAuthFailed``
      or ``ConfigEntryNotReady``.
    * :meth:`complete_platform_setup` - called once per platform; the last
      one in starts the poller.
    * :meth:`set_work_mode` - service entry point; always returns a structured
      response dict, never raises.
    * :meth:`async_entry_updated` - called by the update listener; reports
      whether a reload is needed.
    * :meth:`terminate` - makes every entity unavailable, stops the poller
      and waits up to 5s for cleanup.
    """

    def __init__(
        self,
        token: str | None,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: OAuth2Session | None = None,
    ) -> None:
        """Initialise the controller bound to a single config entry.

        :param token: API key for an API-key entry, ``None`` for a sign-in.
        :param hass: HA core object.
        :param entry: the ``ConfigEntry`` this controller serves.
        :param session: the ``OAuth2Session`` of an account sign-in entry.
        """
        self.hass = hass
        self.entry = entry
        self._api_token = token if isinstance(token, str) and token else None
        self._oauth_session = session
        self.faults = FaultReporter(hass, entry)
        self.poller_task: asyncio.Task[None] | None = None
        # Mapping of API device id -> HybridInverter wrapper.
        self.devices: dict[str, HybridInverter] = {}
        # ``async_add_entities`` callbacks indexed by platform name.
        self._add_entities_callbacks: dict[str, Callable[[list[Any]], None]] = {}
        # Per-platform builders of the entities an inverter found at runtime
        # needs beyond its sensors and binary sensors (the Number entity).
        self._entity_factories: dict[str, Callable[[HybridInverter], list[Any]]] = {}
        # Subscribers pinged when options change or the controller shuts down.
        self._option_listeners: list[Callable[[], None]] = []
        # ``_poll_now`` asks the poller to run its next cycle early;
        # ``_shutdown`` stops the poller and any POST backoff. They are kept
        # separate so a successful command or an option change can never cut
        # another command's retry backoff short.
        self._poll_now = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._terminated = False
        self._initialising = False
        self._auth_failed = False
        # True when the rejection cannot be transient (a refused sign-in
        # refresh), so it is not re-tested.
        self._auth_failure_definitive = False
        self._last_auth_check = 0.0
        # Monotonic time before which polling must not resume (HTTP 429).
        self._polls_deferred_until = 0.0
        # Consecutive successful site listings each known inverter was
        # missing from.
        self._missing_listings: dict[str, int] = {}
        # Devices whose current run of processing failures has been logged
        # with its traceback already.
        self._processing_failures_logged: set[str] = set()
        # ``repr`` of the stored poll interval last reported as unusable.
        self._reported_interval: str | None = None
        # Per-inverter command serialisation: commands to one inverter run one
        # at a time, and a newer command supersedes an older one that is still
        # waiting or retrying, so the last command issued is the one that wins.
        self._command_locks: dict[str, asyncio.Lock] = {}
        self._command_generations: dict[str, int] = {}
        # Debug breadcrumb: the first device payload is logged once per process.
        self._logged_first_payload = False
        # Most recent raw API payloads, retained for the Diagnostics platform.
        # ``None`` distinguishes "never polled" from "polled, empty".
        self._last_site_payload: dict[str, Any] | None = None
        self._last_device_payloads: dict[str, dict[str, Any]] = {}
        # Snapshots used to decide what an entry update means.
        self._configured_device_label = self.device_label_override
        self._options_snapshot: dict[str, Any] = dict(entry.options)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def uses_account_sign_in(self) -> bool:
        """Return True when this entry signs in with an Eleven Energy account."""
        return self._oauth_session is not None

    @property
    def auth_failed(self) -> bool:
        """Return True while the API rejects the credential."""
        return self._auth_failed

    @property
    def terminated(self) -> bool:
        """Return True once :meth:`terminate` has run."""
        return self._terminated

    @property
    def poll_interval(self) -> int:
        """Return the configured poll interval in seconds.

        The stored option is parsed strictly. A value that is not a whole
        number of seconds (a boolean, NaN, text, a fraction) means the
        default, and one outside the allowed range is clamped; either is
        logged once per stored value rather than applied silently.
        """
        raw = self.entry.options.get(CONF_POLL_INTERVAL)
        if raw is None:
            return DEFAULT_POLL_INTERVAL_SECONDS
        parsed = None if isinstance(raw, bool) else parse_whole_number(raw)
        if parsed is None:
            self._report_interval(raw, f"using the default of {DEFAULT_POLL_INTERVAL_SECONDS} s")
            return DEFAULT_POLL_INTERVAL_SECONDS
        clamped = max(MIN_POLL_INTERVAL_SECONDS, min(MAX_POLL_INTERVAL_SECONDS, parsed))
        if clamped != parsed:
            self._report_interval(raw, f"using {clamped} s, the nearest allowed value")
        return clamped

    def _report_interval(self, raw: Any, consequence: str) -> None:
        # ``repr`` tells True from 1 and, unlike ``==``, matches NaN to itself.
        if self._reported_interval == repr(raw):
            return
        self._reported_interval = repr(raw)
        _LOGGER.warning(
            "Eleven Energy Plus stored poll interval %r is not a whole number "
            "of seconds between %d and %d; %s",
            raw,
            MIN_POLL_INTERVAL_SECONDS,
            MAX_POLL_INTERVAL_SECONDS,
            consequence,
        )

    @property
    def device_label_override(self) -> str:
        """Return the user-supplied device-card label, or ``""`` if unset."""
        raw = self.entry.options.get(CONF_DEVICE_LABEL, "")
        if not isinstance(raw, str):
            return ""
        return raw.strip()

    @property
    def last_site_payload(self) -> dict[str, Any] | None:
        """Return the most recent successful ``/site`` payload."""
        return self._last_site_payload

    @property
    def last_device_payloads(self) -> dict[str, dict[str, Any]]:
        """Return the most recent successful ``/devices/<id>`` payload per device."""
        return self._last_device_payloads

    @property
    def registered_platforms(self) -> list[str]:
        """Return the platforms that have registered an add-entities callback."""
        return sorted(self._add_entities_callbacks)

    # ------------------------------------------------------------------
    # Platform plumbing and option changes
    # ------------------------------------------------------------------

    def register_platform(
        self, platform: str, callback: Callable[[list[Any]], None]
    ) -> None:
        """Store a platform's async_add_entities so dynamic entities can be added later."""
        self._add_entities_callbacks[platform] = callback

    def add_entities(self, platform: str, entities: Iterable[Any]) -> None:
        """Add entities for a platform if its callback has been registered.

        Before the platforms have set up there is no callback yet; the
        entities are already held in the inverter's dictionaries, which the
        platform setup reads, so nothing is lost.
        """
        items = list(entities)
        if not items:
            return
        callback = self._add_entities_callbacks.get(platform)
        if callback is None:
            _LOGGER.debug(
                "No add-entities callback for platform %s yet; %d entities "
                "will be added by the platform setup",
                platform,
                len(items),
            )
            return
        callback(items)

    def add_option_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a listener pinged when options change and on shutdown."""
        self._option_listeners.append(listener)

        def _unsub() -> None:
            if listener in self._option_listeners:
                self._option_listeners.remove(listener)

        return _unsub

    def _ping_listeners(self) -> None:
        for listener in list(self._option_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("Eleven Energy Plus option listener raised")

    def notify_options_changed(self) -> None:
        """Wake the poller and ping option listeners after an options change."""
        self._poll_now.set()
        self._ping_listeners()

    def async_entry_updated(self) -> bool:
        """Handle a config entry update; return True when a reload is needed.

        Only *options* matter here. Credential changes come from the
        reauthentication and reconfigure flows, which schedule the reload
        themselves, and from the OAuth session refreshing its access token,
        which must not reload anything. Changing the device-label override
        needs a reload because device info is registered at platform setup;
        any other option (the poll interval) is applied live.
        """
        options = dict(self.entry.options)
        if options == self._options_snapshot:
            return False
        self._options_snapshot = options
        if self.device_label_override != self._configured_device_label:
            return True
        self.notify_options_changed()
        return False

    def complete_platform_setup(
        self,
        platform: str,
        callback: Callable[[list[Any]], None],
        factory: Callable[[HybridInverter], list[Any]] | None = None,
    ) -> None:
        """Record a platform's add-entities callback and maybe start polling.

        ``factory`` builds that platform's entities for an inverter found
        later, at runtime (platforms whose entities do not live on the
        inverter). The poller only starts once *all* expected platforms have
        registered, so the first poll can hand newly-discovered dynamic
        entities to every platform's callback without dropping any. It
        starts exactly once per controller lifetime.
        """
        self.register_platform(platform, callback)
        if factory is not None:
            self._entity_factories[platform] = factory

        expected = {p.value for p in PLATFORMS}
        if (
            expected.issubset(self._add_entities_callbacks.keys())
            and self.poller_task is None
            and not self._terminated
        ):
            self.start_poller()

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _auth_headers(self) -> dict[str, str]:
        """Return request headers with a credential that is valid now."""
        if self._oauth_session is not None:
            try:
                await self._oauth_session.async_ensure_token_valid()
            except OAuth2TokenRequestReauthError as err:
                self._handle_auth_failure(None)
                raise ElevenEnergyAuthError(
                    "Eleven Energy refused to refresh the account sign-in"
                ) from err
            except (OAuth2TokenRequestError, aiohttp.ClientError, TimeoutError) as err:
                raise ElevenEnergyApiError(
                    f"could not refresh the account sign-in: {err}"
                ) from err
            token = self._oauth_session.token.get("access_token")
        else:
            token = self._api_token
        if not token:
            self._handle_auth_failure(None)
            raise ElevenEnergyAuthError("no Eleven Energy credential is configured")
        return {
            "accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, url_suffix: str, *, json: dict[str, Any] | None = None
    ) -> tuple[int, Any, str | None]:
        """Make one API request; return ``(status, body, retry_after)``.

        Raises :class:`ElevenEnergyAuthError` for 401/403 and
        :class:`ElevenEnergyApiError` for transport failures and timeouts.
        Other statuses are returned for the caller to judge.
        """
        headers = await self._auth_headers()
        session = async_get_clientsession(self.hass)
        try:
            async with session.request(
                method,
                f"{BASE_URL}{url_suffix}",
                headers=headers,
                json=json,
                timeout=API_TIMEOUT,
            ) as response:
                status = response.status
                retry_after = response.headers.get("Retry-After")
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ElevenEnergyApiError(
                f"{method} {url_suffix} failed: {err!r}"
            ) from err

        if status in (401, 403):
            self._handle_auth_failure(status)
            raise ElevenEnergyAuthError(
                f"Eleven Energy rejected the credential with status {status}",
                status,
            )
        return status, body, retry_after

    def _handle_auth_failure(self, status: int | None) -> None:
        """React once to a rejected credential.

        Every inverter goes unavailable, since its values can no longer be
        refreshed, and reauthentication is requested. During setup Home
        Assistant starts reauthentication itself from the
        ``ConfigEntryAuthFailed`` that :meth:`initialise` raises.
        """
        if status is None:
            self._auth_failure_definitive = True
        if self._auth_failed:
            return
        self._auth_failed = True
        self._last_auth_check = time.monotonic()
        _LOGGER.error(
            "Eleven Energy Plus credential was rejected%s; reauthentication "
            "is required (Settings > Devices & services)",
            f" (HTTP {status})" if status is not None else "",
        )
        for device in self.devices.values():
            device.mark_unavailable()
        # A rejection is an answer, and polling now pauses, so the
        # unreachable-cloud timer must not run on through it.
        self.faults.report_poll_cycle(True)
        if not self._initialising:
            self.faults.report_credential(True)
            self.entry.async_start_reauth(self.hass)

    def _recover_auth(self) -> None:
        """The API accepts the credential again: undo the rejection."""
        self._auth_failed = False
        self._auth_failure_definitive = False
        _LOGGER.warning(
            "Eleven Energy Plus credential is accepted again; resuming polling"
        )
        for flow in self.hass.config_entries.flow.async_progress_by_handler(DOMAIN):
            context = flow["context"]
            if (
                context.get("source") == SOURCE_REAUTH
                and context.get("entry_id") == self.entry.entry_id
            ):
                self.hass.config_entries.flow.async_abort(flow["flow_id"])
        self.faults.report_credential(False)

    async def _check_auth_recovered(self) -> bool:
        """Re-test a rejected credential when it is due; True if it works again."""
        if self._auth_failure_definitive:
            return False
        now = time.monotonic()
        if now - self._last_auth_check < AUTH_RECOVERY_SECONDS:
            return False
        self._last_auth_check = now
        try:
            await self._fetch_site()
        except ElevenEnergyAuthError:
            _LOGGER.debug("Eleven Energy Plus credential is still rejected")
            return False
        except ElevenEnergyApiError as err:
            _LOGGER.debug("Eleven Energy Plus could not re-test the credential: %s", err)
            return False
        self._recover_auth()
        return True

    def _rate_limited(self, what: str, retry_after: str | None) -> ElevenEnergyRateLimitedError:
        hint = parse_retry_after(retry_after, dt_util.utcnow())
        return ElevenEnergyRateLimitedError(f"{what} was rate limited (HTTP 429)", hint)

    async def _fetch_site(self) -> dict[str, Any]:
        """Return the validated ``/site`` payload."""
        status, body, retry_after = await self._request("GET", "site")
        if status == 429:
            raise self._rate_limited("the site API", retry_after)
        if status != 200:
            raise ElevenEnergyApiError(f"site API responded with {status}")
        if not isinstance(body, dict) or not isinstance(body.get("devices"), list):
            raise ElevenEnergyApiError("site API returned an unexpected payload")
        return body

    async def _fetch_device(self, device_id: str) -> dict[str, Any]:
        """Return the validated ``/devices/<id>`` payload."""
        status, body, retry_after = await self._request("GET", device_path(device_id))
        if status == 429:
            raise self._rate_limited(f"device {device_id}", retry_after)
        if status != 200:
            raise ElevenEnergyApiError(
                f"device {device_id} API responded with {status}"
            )
        if not isinstance(body, dict):
            raise ElevenEnergyApiError(
                f"device {device_id} API returned an unexpected payload"
            )
        reported = body.get("deviceId")
        if reported is not None and str(reported) != device_id:
            raise ElevenEnergyApiError(
                f"device {device_id} API returned the reading of device {reported!r}"
            )
        return body

    def _defer_polls(self, err: ElevenEnergyRateLimitedError) -> None:
        """Hold off polling for as long as the API asked (or a default)."""
        delay = err.retry_after if err.retry_after is not None else RATE_LIMIT_BACKOFF_SECONDS
        self._polls_deferred_until = max(
            self._polls_deferred_until, time.monotonic() + delay
        )
        _LOGGER.warning(
            "Eleven Energy Plus is rate limited; the next poll is in %.0f s", delay
        )

    def _poll_deferral(self) -> float:
        """Return how many seconds polling must still wait for a rate limit."""
        return max(0.0, self._polls_deferred_until - time.monotonic())

    async def _wait_backoff(self, delay: float) -> None:
        """Sleep for ``delay`` seconds, waking early on shutdown."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown.wait(), timeout=delay)

    async def send_reliable_post(
        self,
        url_suffix: str,
        payload: dict[str, Any],
        *,
        superseded: Callable[[], bool] | None = None,
    ) -> PostResult:
        """POST with a bounded, cancellable retry policy.

        Retries only what retrying can fix - network errors, timeouts, 408,
        429 and 5xx - with a doubling delay (1, 2, 4, 8 s; ``Retry-After``,
        as seconds or an HTTP date, may lengthen one wait up to 30 s) for at
        most :data:`_MAX_POST_ATTEMPTS` attempts. Stops early on shutdown, or
        when ``superseded()`` reports that a newer command has been issued.
        """
        last_status: int | None = None
        # Every way out of the loop below sets both.
        error: str
        kind: ErrorKind
        delay = 1.0
        attempts = 0

        # Every way out is an explicit ``break`` (or the return on success).
        while True:
            if self._shutdown.is_set():
                error = "aborted because Eleven Energy Plus is shutting down"
                kind = ErrorKind.ABORTED
                break
            if superseded is not None and superseded():
                error = "superseded by a newer work-mode request for this inverter"
                kind = ErrorKind.SUPERSEDED
                break

            attempts += 1
            retry_after: str | None = None
            try:
                status, body, retry_after = await self._request(
                    "POST", url_suffix, json=payload
                )
            except ElevenEnergyAuthError as err:
                last_status = err.status
                error = str(err)
                kind = ErrorKind.CREDENTIAL_REJECTED
                break
            except ElevenEnergyApiError as err:
                error = str(err)
                kind = ErrorKind.API_ERROR
                retryable = True
            else:
                last_status = status
                if status == 200:
                    return PostResult(status=200, body=body, attempts=attempts)
                error = f"API returned status {status}"
                kind = ErrorKind.API_ERROR
                retryable = status in _RETRYABLE_STATUSES or status >= 500

            if not retryable or attempts >= _MAX_POST_ATTEMPTS:
                break

            wait = delay
            hinted = parse_retry_after(retry_after, dt_util.utcnow())
            if hinted is not None:
                wait = min(max(wait, hinted), _MAX_BACKOFF_SECONDS)
            _LOGGER.info(
                "POST to %s failed (%s, attempt %d/%d); retrying after %.0fs",
                url_suffix,
                error,
                attempts,
                _MAX_POST_ATTEMPTS,
                wait,
            )
            await self._wait_backoff(wait)
            delay *= 2

        return PostResult(
            status=last_status, body=None, attempts=attempts, error=error, kind=kind
        )

    # ------------------------------------------------------------------
    # Work modes
    # ------------------------------------------------------------------

    def _sanitise_work_mode(
        self, mode: str, data: dict[str, Any]
    ) -> _WorkModeRequest:
        """Translate a service-call payload into user and API parameters.

        The service schema has already validated types and ranges; the
        checks here repeat them for direct callers. A value that is not
        usable exactly as given - out of range, not a number, an ambiguous
        boolean - is recorded in ``invalid`` and the command is refused,
        rather than clamped, guessed or sent with that field missing.
        """
        request = _WorkModeRequest()

        def _store(user_key: str, api_key: str, parse: Callable[[Any], Any]) -> None:
            if user_key not in data or data[user_key] is None:
                return
            parsed = parse(data[user_key])
            if parsed is None:
                request.invalid.append(user_key)
                return
            request.user_params[user_key] = parsed
            request.api_params[api_key] = parsed

        match mode:
            case "set_work_mode_self_consumption":
                _store("percent_to_battery", "targetExcessPc", _valid_percent)
            case "set_work_mode_force_charge":
                _store("target_percent", "targetSoc", _valid_percent)
                _store("target_power", "rate", _valid_power_kw)
            case "set_work_mode_grid_export":
                _store("target_percent", "targetSoc", _valid_percent)
                _store("target_power", "rate", _valid_power_kw)
                _store("include_excess_solar", "addAverageExcess", parse_bool)
                _store("overdrive", "overdrive", parse_bool)
            case "set_work_mode_idle_battery":
                _store("allow_charging", "allowCharge", parse_bool)
                _store("allow_discharging", "allowDischarge", parse_bool)
            case "set_work_mode_target_soc":
                _store("target_soc", "targetSoc", _valid_percent)
                _store("max_charge_power", "maxChargeRate", _valid_power_kw)
                _store("max_discharge_power", "maxDischargeRate", _valid_power_kw)
                _store("target_minutes", "targetMinutes", _valid_minutes)
        # pv_export and reset take no parameters.
        return request

    def _resolve_target_device(
        self, data: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Resolve the Eleven Energy device id a service call is aimed at.

        Returns ``(device_id, None)`` on success or ``(None, reason)``.

        * ``device_id`` (the Inverter field, or a device target), ``entity_id``
          (resolved to the entity's device) and ``area_id`` (devices and
          entities in the area) are all accepted. When any is given it must
          match exactly one Eleven Energy Plus inverter; a target that matches
          none, or several, is refused rather than guessed.
        * With no target, a site with one inverter uses it. A site with more
          than one refuses, because picking "the first" would control the
          wrong machine.
        """
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)

        for unsupported in ("floor_id", "label_id"):
            if _as_list(data.get(unsupported)):
                return None, (
                    f"{unsupported} targets are not supported; choose the "
                    "inverter in the Inverter field"
                )

        explicit = False
        ha_device_ids: list[str] = []

        device_ids = _as_list(data.get("device_id"))
        if device_ids:
            explicit = True
            ha_device_ids.extend(device_ids)

        entity_ids = _as_list(data.get("entity_id"))
        if entity_ids:
            explicit = True
            for entity_id in entity_ids:
                reg_entry = ent_reg.async_get(entity_id)
                if reg_entry is not None and reg_entry.device_id is not None:
                    ha_device_ids.append(reg_entry.device_id)

        area_ids = _as_list(data.get("area_id"))
        if area_ids:
            explicit = True
            for area_id in area_ids:
                ha_device_ids.extend(
                    device.id for device in dr.async_entries_for_area(dev_reg, area_id)
                )
                ha_device_ids.extend(
                    reg_entry.device_id
                    for reg_entry in er.async_entries_for_area(ent_reg, area_id)
                    if reg_entry.device_id is not None
                )

        if explicit:
            matched: set[str] = set()
            for hass_device_id in ha_device_ids:
                device = dev_reg.async_get(hass_device_id)
                if device is None:
                    continue
                for domain, identifier in device.identifiers:
                    if domain == DOMAIN and identifier in self.devices:
                        matched.add(identifier)
            if not matched:
                return None, "the target does not match any Eleven Energy Plus inverter"
            if len(matched) > 1:
                return None, (
                    "the target matches more than one inverter; call the action "
                    "once per inverter"
                )
            return matched.pop(), None

        inverters = [
            device.device_id
            for device in self.devices.values()
            if device.type == "hybridinverter"
        ]
        if len(inverters) == 1:
            return inverters[0], None
        if not inverters:
            return None, "no Eleven Energy Plus device available"
        return None, (
            "this site has more than one inverter; choose one in the Inverter field"
        )

    def _device_name(self, device_id: str | None) -> str:
        device = self.devices.get(device_id) if device_id is not None else None
        return device.display_name if device is not None else "Eleven Energy Plus"

    async def set_work_mode(self, mode: str, data: dict[str, Any]) -> dict[str, Any]:
        """Change an inverter's work mode.

        Called by the service handler for every ``set_work_mode_*`` service.
        Always returns a response dict and **never raises**: even a bug in
        the integration is caught, logged with its traceback, reported as a
        repair issue and returned as ``internal_error``. The handler decides
        whether a failure becomes an exception (when the caller did not ask
        for a response) or is returned.

        Response schema (stable, matches the table in ``README.md``)::

            {
                "success":    bool,          # True iff the API returned 200.
                "status":     int | None,    # Last HTTP status, None if no request made.
                "device_id":  str | None,    # Eleven Energy device id targeted.
                "work_mode":  str | None,    # Canonical API work mode posted.
                "params":     dict,          # Sanitised user-facing parameters.
                "attempts":   int,           # POST attempts made (0..5).
                "error":      str | None,    # Human-readable failure reason.
                "error_kind": str | None,    # ErrorKind value; None on success.
            }

        On success the poller is woken so the new mode shows up promptly.
        """
        result: dict[str, Any] = {
            "success": False,
            "status": None,
            "device_id": None,
            "work_mode": None,
            "params": {},
            "attempts": 0,
            "error": None,
            "error_kind": None,
        }
        try:
            await self._set_work_mode(mode, data, result)
        except Exception as err:
            # ``success`` is only ever set as the last step, so it is False.
            _LOGGER.exception("Eleven Energy Plus failed running %s", mode)
            result["error"] = f"internal error ({type(err).__name__}: {err})"
            result["error_kind"] = ErrorKind.INTERNAL_ERROR

        owner = self._running_controller()
        device_id = result["device_id"]
        kind = result["error_kind"]
        if owner is None or (device_id is not None and device_id not in owner.devices):
            # Nothing is running the entry, or the inverter was deleted while
            # the command was in flight: nothing would clear an issue.
            return result
        if result["success"]:
            owner.faults.report_work_mode_result(device_id, owner._device_name(device_id), None)
            # A failure no inverter was resolved for: commands work again.
            owner.faults.report_work_mode_result(_SITE_ISSUE_TARGET, owner._device_name(None), None)
            owner.faults.record_work_mode(device_id, mode)
        elif kind in _ISSUE_KINDS:
            owner.faults.report_work_mode_result(
                device_id or _SITE_ISSUE_TARGET, owner._device_name(device_id), str(result["error"])
            )
        return result

    def _running_controller(self) -> Controller | None:
        """Return the controller now running this entry, if any.

        A command can outlive its controller (the entry was unloaded or
        reloaded while it was being sent); its outcome then belongs to the
        controller that replaced this one, which alone can clear what it
        raises and owns the stored optimiser timers.
        """
        if not self._terminated:
            return self
        current = getattr(self.entry, "runtime_data", None)
        if isinstance(current, Controller) and not current.terminated:
            return current
        return None

    async def _set_work_mode(
        self, mode: str, data: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Fill in ``result`` for :meth:`set_work_mode`."""

        def _refuse(kind: ErrorKind, reason: str) -> None:
            _LOGGER.warning("Not changing work mode (%s): %s", mode, reason)
            result["error"] = reason
            result["error_kind"] = kind

        api_work_mode = _API_WORK_MODE_NAMES.get(mode)
        if api_work_mode is None:
            _refuse(ErrorKind.INVALID_REQUEST, f"unknown service {mode}")
            return
        result["work_mode"] = api_work_mode

        device_id, target_error = self._resolve_target_device(data)
        if device_id is None:
            _refuse(ErrorKind.INVALID_REQUEST, str(target_error))
            return
        result["device_id"] = device_id

        if self._auth_failed:
            _refuse(
                ErrorKind.CREDENTIAL_REJECTED,
                "the Eleven Energy credential was rejected; reauthenticate the "
                "integration first",
            )
            return

        missing = [
            key for key in _REQUIRED_FIELDS[mode] if data.get(key) is None
        ]
        if missing:
            _refuse(
                ErrorKind.INVALID_REQUEST,
                f"missing required field(s): {', '.join(missing)}",
            )
            return

        request = self._sanitise_work_mode(mode, data)
        if request.invalid:
            _refuse(
                ErrorKind.INVALID_REQUEST,
                f"invalid value for field(s): {', '.join(request.invalid)}",
            )
            return
        result["params"] = request.user_params

        api_payload = {**request.api_params, "workMode": api_work_mode}

        generation = self._command_generations.get(device_id, 0) + 1
        self._command_generations[device_id] = generation

        def _superseded() -> bool:
            return self._command_generations.get(device_id) != generation

        lock = self._command_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            if _superseded():
                _refuse(
                    ErrorKind.SUPERSEDED,
                    "superseded by a newer work-mode request for this inverter",
                )
                return
            post_result = await self.send_reliable_post(
                device_path(device_id, "/operatingMode"),
                api_payload,
                superseded=_superseded,
            )

        result["status"] = post_result.status
        result["attempts"] = post_result.attempts

        if post_result.status == 200:
            result["success"] = True
            self._poll_now.set()
            return
        result["error"] = post_result.error or (
            f"API returned status {post_result.status}"
        )
        result["error_kind"] = post_result.kind or ErrorKind.API_ERROR
        _LOGGER.warning(
            "Unable to change work mode to %s on %s: %s",
            api_work_mode,
            device_id,
            result["error"],
        )

    # ------------------------------------------------------------------
    # Discovery and polling
    # ------------------------------------------------------------------

    async def initialise(self) -> None:
        """Discover the site's inverters, reading each once.

        Raises :class:`ConfigEntryAuthFailed` when the credential is rejected
        (no amount of retrying fixes it; Home Assistant asks the user to
        reauthenticate), and :class:`ConfigEntryNotReady` when the API cannot
        be reached or lists no hybrid inverters, so Home Assistant retries.
        """
        _LOGGER.info("Eleven Energy Plus initialising")
        await self.faults.async_load()
        self._initialising = True
        try:
            payload = await self._fetch_site()
            self._last_site_payload = payload
            await self._reconcile_site(payload)
        except ElevenEnergyAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ElevenEnergyApiError as err:
            raise ConfigEntryNotReady(
                f"Eleven Energy site API was unreachable during setup: {err}"
            ) from err
        finally:
            self._initialising = False

        if not self.devices:
            raise ConfigEntryNotReady(
                "Eleven Energy site returned no hybrid inverter devices"
            )

    async def poll_site(self) -> bool:
        """Poll the site endpoint, picking up new and departed inverters.

        Returns ``True`` only when the API answered with a usable payload.
        Never raises for API problems; a rejected credential has already
        started reauthentication by the time this returns ``False``.
        """
        try:
            payload = await self._fetch_site()
        except ElevenEnergyAuthError:
            return False
        except ElevenEnergyRateLimitedError as err:
            self._defer_polls(err)
            return False
        except ElevenEnergyApiError as err:
            _LOGGER.warning("Eleven Energy Plus site poll failed: %s", err)
            return False
        self._last_site_payload = payload
        try:
            await self._reconcile_site(payload)
        except ElevenEnergyAuthError:
            # Rejected while reading a newly listed inverter.
            return False
        return True

    async def _reconcile_site(self, payload: dict[str, Any]) -> None:
        """Bring the known inverters in line with a site listing.

        Each newly listed machine is read once before its entities are built:
        the site listing says what model it is, but how many batteries are
        fitted is only in the reading. A failed read does not stop discovery
        - the inverter is created from its listing and picks the rest up on
        the first successful poll. Devices whose ``type`` isn't
        ``"hybridinverter"`` (heat pumps, EV chargers, ...) are ignored.

        If the platforms have already set up, the new inverter's entities on
        every platform are registered immediately.

        A known inverter absent from this listing counts one listing towards
        :data:`~.const.MISSING_AFTER_SITE_POLLS`; present again, it is back.
        """
        listed = listed_inverter_ids(payload)
        self._track_missing(listed)
        summaries = {
            summary["deviceId"]: summary
            for summary in payload["devices"]
            if isinstance(summary, dict) and summary.get("deviceId") in listed
        }
        # Sorted, so which of two namesakes is numbered does not depend on
        # the order the API lists them in.
        for device_id in sorted(listed):
            if device_id in self.devices:
                continue
            await self._discover(device_id, summaries[device_id])

    def _track_missing(self, listed: list[str]) -> None:
        for device_id, device in self.devices.items():
            if device_id in listed:
                self._missing_listings.pop(device_id, None)
                if device.missing:
                    device.set_missing(False)
                    self.faults.report_missing(device_id, device.display_name, False)
                continue
            count = self._missing_listings.get(device_id, 0) + 1
            self._missing_listings[device_id] = count
            if count >= MISSING_AFTER_SITE_POLLS and not device.missing:
                device.set_missing(True)
                # Its online flag goes unread while it is missing: the
                # offline timer restarts when it is back.
                self.faults.report_offline(device_id, device.display_name, False)
                self.faults.report_missing(device_id, device.display_name, True)

    async def _discover(self, device_id: str, summary: dict[str, Any]) -> None:
        """Create the inverter for a newly listed device."""
        _LOGGER.debug(
            "Eleven Energy Plus discovered inverter %s; raw site payload: %s",
            device_id,
            summary,
        )

        state: dict[str, Any] | None = None
        if self._poll_deferral() > 0:
            _LOGGER.info(
                "Eleven Energy Plus is rate limited; device %s is read once "
                "polling resumes",
                device_id,
            )
        else:
            try:
                state = await self._fetch_device(device_id)
            except ElevenEnergyRateLimitedError as err:
                self._defer_polls(err)
            except ElevenEnergyApiError as err:
                _LOGGER.warning(
                    "Could not read Eleven Energy Plus device %s during "
                    "discovery: %s",
                    device_id,
                    err,
                )

        label = api_device_label(summary, state)
        override = self.device_label_override
        if override:
            _LOGGER.info(
                "Eleven Energy Plus applying device label override for %s: "
                "%r (API said %r)",
                device_id,
                override,
                label,
            )
            label = override

        # Two inverters of one model, or any two under the label override,
        # would share a name; the Inverter field and the repair texts must
        # tell them apart.
        taken = {known.display_name.casefold() for known in self.devices.values()}
        ordinal = 1
        while device_card_name(label, ordinal).casefold() in taken:
            ordinal += 1

        serial = summary.get("serialNumber")
        inverter = HybridInverter(
            self.hass,
            self,
            device_id,
            label,
            serial if isinstance(serial, str) else "",
            model=model_block(summary, state),
            state=state,
            ordinal=ordinal,
        )
        self.devices[device_id] = inverter
        _LOGGER.info(
            "Created inverter %s (label=%r, %s phase, %d MPPT, %d batter%s)",
            device_id,
            label,
            3 if inverter.three_phase else 1,
            inverter.mppt_count,
            len(inverter.battery_indices),
            "y" if len(inverter.battery_indices) == 1 else "ies",
        )

        # Register the curated entities first (a no-op before the platforms
        # exist), then apply the reading, which may add dynamic entities
        # through the same callbacks without registering any twice.
        self.add_entities("sensor", inverter.sensor_entities.values())
        self.add_entities("binary_sensor", inverter.binary_sensor_entities.values())
        for platform, factory in self._entity_factories.items():
            self.add_entities(platform, factory(inverter))
        if state is not None:
            self._apply_device_payload(inverter, state)

    def _apply_device_payload(
        self, device: HybridInverter, payload: dict[str, Any]
    ) -> None:
        """Feed a successful device reading into its inverter.

        If processing the reading raises, it counts as a failed poll: the
        values on show did not come from this reading, so treating it as a
        success would present them as current.
        """
        self._last_device_payloads[device.device_id] = payload

        if not self._logged_first_payload:
            rendered = repr(payload)
            if len(rendered) > _PAYLOAD_LOG_LIMIT:
                rendered = f"{rendered[:_PAYLOAD_LOG_LIMIT]}... (truncated)"
            _LOGGER.debug(
                "Eleven Energy Plus first device payload for %s: %s",
                device.device_id,
                rendered,
            )
            self._logged_first_payload = True

        try:
            device.update(payload)
        except Exception:
            if device.device_id in self._processing_failures_logged:
                _LOGGER.debug(
                    "Eleven Energy Plus still failing to process device %s",
                    device.device_id,
                )
            else:
                self._processing_failures_logged.add(device.device_id)
                _LOGGER.exception(
                    "Failed processing Eleven Energy Plus device %s update; "
                    "counting it as a failed poll",
                    device.device_id,
                )
            device.record_poll_failure()
            return
        self._processing_failures_logged.discard(device.device_id)
        device.record_poll_success()
        self.faults.report_offline(device.device_id, device.display_name, device.offline)

    async def poll_devices(self, *, unread_only: bool = False) -> bool:
        """Poll every listed device for fresh telemetry.

        Returns True when at least one device answered. Per-device failures
        are isolated: a failure for one device is logged, counted towards
        that device's availability, and the loop moves on. A rejected
        credential stops the cycle, since every further request would be
        rejected too, and so does rate limiting. Inverters missing from the
        site listing are not polled, and with ``unread_only`` neither are
        those already read.
        """
        reached = False
        for device in list(self.devices.values()):
            if self._shutdown.is_set():
                break
            if device.missing or (
                unread_only and device.device_id in self._last_device_payloads
            ):
                continue
            try:
                payload = await self._fetch_device(device.device_id)
            except ElevenEnergyAuthError:
                break
            except ElevenEnergyRateLimitedError as err:
                device.record_poll_failure()
                self._defer_polls(err)
                reached = True
                break
            except ElevenEnergyApiError as err:
                _LOGGER.warning(
                    "Failed polling Eleven Energy Plus device %s: %s",
                    device.device_id,
                    err,
                )
                device.record_poll_failure()
                continue
            reached = True
            if self.devices.get(device.device_id) is not device:
                # Deleted while it was being read.
                continue
            self._apply_device_payload(device, payload)
        return reached

    async def _poll_cycle(self) -> None:
        """Run one poll cycle: re-test a rejected credential, site, devices."""
        if self._auth_failed and not await self._check_auth_recovered():
            return
        site_ok = await self.poll_site()
        if self._auth_failed:
            return
        # A rate-limited site poll means the device polls would be too.
        devices_ok = self._poll_deferral() == 0 and await self.poll_devices()
        if self._auth_failed or self._shutdown.is_set():
            return
        # Being rate limited is an answer: the API is reachable.
        self.faults.report_poll_cycle(site_ok or devices_ok or self._poll_deferral() > 0)
        self.faults.check_optimiser(
            {device_id: device.display_name for device_id, device in self.devices.items()}
        )

    def start_poller(self) -> None:
        """Start the background polling task.

        The poller first reads, straight away, any inverter that discovery
        could not read, so its entities populate without waiting one full
        cadence. Inverters discovery did read are not read again at once: a
        second copy of the same reading would count as confirming it, which
        matters to a counter whose first reading after a restart is lower
        than before (see :class:`~.hybrid_inverter.TotalIncreasingGuard`).
        It then loops on the poll-now event with a
        timeout equal to the current :attr:`poll_interval`, lengthened while
        the API is rate limiting. Each iteration polls the site (for added
        and departed devices) and then each device. All per-iteration errors
        are caught and logged so a transient API blip cannot kill the task;
        cancellation is not caught. While the credential is rejected the
        poller only re-tests it, every
        :data:`~.const.AUTH_RECOVERY_SECONDS`.

        Registered via :meth:`ConfigEntry.async_create_background_task` so
        Home Assistant cancels it during a normal unload even if our own
        :meth:`terminate` is skipped.
        """

        async def periodic() -> None:
            try:
                if self._poll_deferral() == 0:
                    await self.poll_devices(unread_only=True)
            except Exception:
                _LOGGER.exception("Eleven Energy Plus initial device poll failed")

            while not self._shutdown.is_set():
                wait = max(float(self.poll_interval), self._poll_deferral())
                _LOGGER.debug(
                    "Eleven Energy Plus next poll in %.0fs (or sooner on request)",
                    wait,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._poll_now.wait(), timeout=wait)
                self._poll_now.clear()

                if self._shutdown.is_set():
                    return
                if self._poll_deferral() > 0:
                    continue

                try:
                    await self._poll_cycle()
                except Exception:
                    _LOGGER.exception("Eleven Energy Plus poll cycle failed")

        self.poller_task = self.entry.async_create_background_task(
            self.hass, periodic(), "Eleven Energy Plus Poll"
        )

    def forget_device(self, device_id: str) -> None:
        """Drop an inverter whose device the user deleted."""
        device = self.devices.pop(device_id, None)
        if device is not None:
            device.shut_down()
        self._missing_listings.pop(device_id, None)
        self._last_device_payloads.pop(device_id, None)
        self.faults.forget_device(device_id)

    async def terminate(self) -> None:
        """Stop the controller.

        Every entity goes unavailable first, so nothing - not even after an
        unload that fails - is left showing a value that will never update.
        Then shutdown is signalled (which also aborts any POST backoff), the
        poller task is cancelled and given up to 5 seconds to settle, so
        unloads and reloads don't leave a zombie poll racing against a fresh
        controller for the same entities, and the repair issues and
        notifications are withdrawn.
        """
        self._terminated = True
        for device in self.devices.values():
            device.shut_down()
        self._ping_listeners()
        self._shutdown.set()
        self._poll_now.set()
        task = self.poller_task
        self.poller_task = None
        if task is not None:
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=5)
            if task in done and not task.cancelled() and task.exception() is not None:
                _LOGGER.error(
                    "Eleven Energy Plus poll task raised during termination",
                    exc_info=task.exception(),
                )
        await self.faults.async_unload()
        _LOGGER.info("Eleven Energy Plus is no longer polling")
