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
* A small option-listener pub-sub used by the Number entity so it can
  rebroadcast cadence changes triggered via the OptionsFlow.

Failure handling, in one place:

* The API rejecting the credential (HTTP 401/403, or an account sign-in
  whose refresh is refused) raises ``ConfigEntryAuthFailed`` during setup
  and starts Home Assistant's reauthentication flow at runtime. Every
  inverter goes unavailable at once and polling pauses until the entry is
  reloaded with a working credential.
* Anything that merely stops us reaching the API raises
  ``ConfigEntryNotReady`` during setup, so Home Assistant retries on its
  own schedule. At runtime, a device goes unavailable after
  :data:`~.const.UNAVAILABLE_AFTER_FAILED_POLLS` consecutive failed polls.

Everything here is async and cancellation-safe: the poller waits on a
``poll now`` event (set by option changes and successful commands), and
both the poller and the POST backoff wake immediately on the separate
``shutdown`` event that :meth:`terminate` sets.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
import math
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
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
    PLATFORMS,
)
from .hybrid_inverter import HybridInverter

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


def _finite_float(value: Any) -> float | None:
    """Coerce ``value`` to a finite float, or ``None``.

    Booleans are refused (``True`` is not 1 %), and NaN / infinity are
    refused because ``min``/``max`` clamping silently turns NaN into the
    upper bound - i.e. "nan" kW would become full power.
    """
    if isinstance(value, bool):
        return None
    try:
        coerced = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return coerced if math.isfinite(coerced) else None


def _clamp_percent(value: Any) -> float | None:
    """Coerce a percent-like input into 0-100, returning None when uncoercible."""
    coerced = _finite_float(value)
    if coerced is None:
        return None
    return max(0.0, min(100.0, coerced))


def _clamp_power_kw(value: Any) -> float | None:
    """Coerce a kW-like input into the API's range, returning None when uncoercible."""
    coerced = _finite_float(value)
    if coerced is None:
        return None
    return max(MIN_POWER_KW, min(MAX_POWER_KW, coerced))


def _clamp_minutes(value: Any) -> int | None:
    """Coerce a minutes-like input into the API's whole-minute range."""
    coerced = _finite_float(value)
    if coerced is None:
        return None
    return max(MIN_TARGET_MINUTES, min(MAX_TARGET_MINUTES, int(coerced)))


def _coerce_bool(value: Any) -> bool:
    """Best-effort coercion to bool that handles "true"/"false" strings sent over YAML."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


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


class ElevenEnergyAuthError(Exception):
    """The API rejected the credential, or it could not be refreshed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Store the HTTP status that caused the failure, if there was one."""
        super().__init__(message)
        self.status = status


class ElevenEnergyApiError(Exception):
    """The API could not be reached or returned something unusable."""


@dataclass(frozen=True)
class PostResult:
    """Outcome of a ``send_reliable_post`` call."""

    status: int | None
    body: Any | None
    attempts: int
    error: str | None = None


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
    * :meth:`terminate` - stops the poller and waits up to 5s for cleanup.
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
        self.poller_task: asyncio.Task[None] | None = None
        # Mapping of API device id -> HybridInverter wrapper.
        self.devices: dict[str, HybridInverter] = {}
        # ``async_add_entities`` callbacks indexed by platform name.
        self._add_entities_callbacks: dict[str, Callable[[list[Any]], None]] = {}
        # Subscribers to in-process option changes (the Number entity).
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
        """Return True once the API has rejected the credential."""
        return self._auth_failed

    @property
    def configured_device_label(self) -> str:
        """Return the device-label override snapshot from construction time."""
        return self._configured_device_label

    @property
    def poll_interval(self) -> int:
        """Return the currently-configured poll interval, clamped."""
        raw = self.entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_POLL_INTERVAL_SECONDS
        return max(MIN_POLL_INTERVAL_SECONDS, min(MAX_POLL_INTERVAL_SECONDS, value))

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
        """Register a listener invoked when integration options change."""
        self._option_listeners.append(listener)

        def _unsub() -> None:
            if listener in self._option_listeners:
                self._option_listeners.remove(listener)

        return _unsub

    def notify_options_changed(self) -> None:
        """Wake the poller and ping option listeners after an options change."""
        self._poll_now.set()
        for listener in list(self._option_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Eleven Energy Plus option listener raised")

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
        self, platform: str, callback: Callable[[list[Any]], None]
    ) -> None:
        """Record a platform's add-entities callback and maybe start polling.

        The poller only starts once *all* expected platforms have registered,
        so the first poll can hand newly-discovered dynamic entities to every
        platform's callback without dropping any. It starts exactly once per
        controller lifetime.
        """
        self.register_platform(platform, callback)

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
        if self._auth_failed:
            return
        self._auth_failed = True
        _LOGGER.error(
            "Eleven Energy Plus credential was rejected%s; reauthentication "
            "is required (Settings > Devices & services)",
            f" (HTTP {status})" if status is not None else "",
        )
        for device in self.devices.values():
            device.mark_unavailable()
        if not self._initialising:
            self.entry.async_start_reauth(self.hass)

    async def _fetch_site(self) -> dict[str, Any]:
        """Return the validated ``/site`` payload."""
        status, body, _ = await self._request("GET", "site")
        if status != 200:
            raise ElevenEnergyApiError(f"site API responded with {status}")
        if not isinstance(body, dict) or not isinstance(body.get("devices"), list):
            raise ElevenEnergyApiError("site API returned an unexpected payload")
        return body

    async def _fetch_device(self, device_id: str) -> dict[str, Any]:
        """Return the validated ``/devices/<id>`` payload."""
        status, body, _ = await self._request("GET", f"devices/{device_id}")
        if status != 200:
            raise ElevenEnergyApiError(
                f"device {device_id} API responded with {status}"
            )
        if not isinstance(body, dict):
            raise ElevenEnergyApiError(
                f"device {device_id} API returned an unexpected payload"
            )
        return body

    async def _wait_backoff(self, delay: float) -> None:
        """Sleep for ``delay`` seconds, waking early on shutdown."""
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def send_reliable_post(
        self,
        url_suffix: str,
        payload: dict[str, Any],
        *,
        superseded: Callable[[], bool] | None = None,
    ) -> PostResult:
        """POST with a bounded, cancellable retry policy.

        Retries only what retrying can fix - network errors, timeouts, 408,
        429 and 5xx - with a doubling delay (1, 2, 4, 8 s; ``Retry-After``
        may lengthen one wait up to 30 s) for at most
        :data:`_MAX_POST_ATTEMPTS` attempts. Stops early on shutdown, or when
        ``superseded()`` reports that a newer command has been issued.
        """
        last_status: int | None = None
        last_body: Any | None = None
        error: str | None = None
        delay = 1.0
        attempts = 0

        while attempts < _MAX_POST_ATTEMPTS:
            if self._shutdown.is_set():
                error = "aborted because Eleven Energy Plus is shutting down"
                break
            if superseded is not None and superseded():
                error = "superseded by a newer work-mode request for this inverter"
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
                break
            except ElevenEnergyApiError as err:
                error = str(err)
                retryable = True
            else:
                last_status, last_body = status, body
                if status == 200:
                    return PostResult(status=200, body=body, attempts=attempts)
                error = f"API returned status {status}"
                retryable = status in _RETRYABLE_STATUSES or status >= 500

            if not retryable or attempts >= _MAX_POST_ATTEMPTS:
                break

            wait = delay
            if retry_after is not None:
                hinted = _finite_float(retry_after)
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
            status=last_status, body=last_body, attempts=attempts, error=error
        )

    # ------------------------------------------------------------------
    # Work modes
    # ------------------------------------------------------------------

    def _sanitise_work_mode(
        self, mode: str, data: dict[str, Any]
    ) -> _WorkModeRequest:
        """Translate a service-call payload into user and API parameters.

        The service schema has already validated types and ranges, so the
        clamps below are defence in depth for direct callers. A value that
        cannot be used at all is recorded in ``invalid`` and the command is
        refused, rather than sent with that field silently missing.
        """
        request = _WorkModeRequest()

        def _store(user_key: str, api_key: str, coerce: Callable[[Any], Any]) -> None:
            if user_key not in data or data[user_key] is None:
                return
            coerced = coerce(data[user_key])
            if coerced is None:
                request.invalid.append(user_key)
                return
            request.user_params[user_key] = coerced
            request.api_params[api_key] = coerced

        match mode:
            case "set_work_mode_self_consumption":
                _store("percent_to_battery", "targetExcessPc", _clamp_percent)
            case "set_work_mode_force_charge":
                _store("target_percent", "targetSoc", _clamp_percent)
                _store("target_power", "rate", _clamp_power_kw)
            case "set_work_mode_grid_export":
                _store("target_percent", "targetSoc", _clamp_percent)
                _store("target_power", "rate", _clamp_power_kw)
                _store("include_excess_solar", "addAverageExcess", _coerce_bool)
                _store("overdrive", "overdrive", _coerce_bool)
            case "set_work_mode_pv_export":
                pass
            case "set_work_mode_idle_battery":
                _store("allow_charging", "allowCharge", _coerce_bool)
                _store("allow_discharging", "allowDischarge", _coerce_bool)
            case "set_work_mode_target_soc":
                _store("target_soc", "targetSoc", _clamp_percent)
                _store("max_charge_power", "maxChargeRate", _clamp_power_kw)
                _store("max_discharge_power", "maxDischargeRate", _clamp_power_kw)
                _store("target_minutes", "targetMinutes", _clamp_minutes)
            case "set_work_mode_reset":
                pass

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

    async def set_work_mode(self, mode: str, data: dict[str, Any]) -> dict[str, Any]:
        """Change an inverter's work mode.

        Called by the service handler for every ``set_work_mode_*`` service.
        Always returns a response dict and **never raises**; the handler
        decides whether a failure becomes an exception (when the caller did
        not ask for a response) or is returned.

        Response schema (stable, matches the table in ``README.md``)::

            {
                "success":   bool,          # True iff the API returned 200.
                "status":    int | None,    # Last HTTP status, None if no request made.
                "device_id": str | None,    # Eleven Energy device id targeted.
                "work_mode": str | None,    # Canonical API work mode posted.
                "params":    dict,          # Sanitised user-facing parameters.
                "attempts":  int,           # POST attempts made (0..5).
                "error":     str | None,    # Human-readable failure reason.
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
        }

        api_work_mode = _API_WORK_MODE_NAMES.get(mode)
        if api_work_mode is None:
            _LOGGER.warning("Unable to determine work mode from %s", mode)
            result["error"] = f"unknown service {mode}"
            return result
        result["work_mode"] = api_work_mode

        device_id, target_error = self._resolve_target_device(data)
        if device_id is None:
            _LOGGER.warning("Not changing work mode (%s): %s", mode, target_error)
            result["error"] = target_error
            return result
        result["device_id"] = device_id

        if self._auth_failed:
            result["error"] = (
                "the Eleven Energy credential was rejected; reauthenticate the "
                "integration first"
            )
            return result

        missing = [
            key for key in _REQUIRED_FIELDS.get(mode, ()) if data.get(key) is None
        ]
        if missing:
            joined = ", ".join(missing)
            _LOGGER.warning(
                "Aborting %s; required field(s) missing: %s", mode, joined
            )
            result["error"] = f"missing required field(s): {joined}"
            return result

        request = self._sanitise_work_mode(mode, data)
        if request.invalid:
            joined = ", ".join(request.invalid)
            _LOGGER.warning("Aborting %s; invalid value for: %s", mode, joined)
            result["error"] = f"invalid value for field(s): {joined}"
            return result
        result["params"] = request.user_params

        api_payload = {**request.api_params, "workMode": api_work_mode}

        generation = self._command_generations.get(device_id, 0) + 1
        self._command_generations[device_id] = generation

        def _superseded() -> bool:
            return self._command_generations.get(device_id) != generation

        lock = self._command_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            if _superseded():
                result["error"] = (
                    "superseded by a newer work-mode request for this inverter"
                )
                return result
            post_result = await self.send_reliable_post(
                f"devices/{device_id}/operatingMode",
                api_payload,
                superseded=_superseded,
            )

        result["status"] = post_result.status
        result["attempts"] = post_result.attempts

        if post_result.status == 200:
            result["success"] = True
            self._poll_now.set()
        else:
            result["error"] = post_result.error or (
                f"API returned status {post_result.status}"
            )
            _LOGGER.warning(
                "Unable to change work mode to %s on %s: %s",
                api_work_mode,
                device_id,
                result["error"],
            )

        return result

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
        """Poll the site endpoint and pick up newly-added inverters.

        Returns ``True`` only when the API answered with a usable payload.
        Never raises for API problems; a rejected credential has already
        started reauthentication by the time this returns ``False``.
        """
        try:
            payload = await self._fetch_site()
            self._last_site_payload = payload
            await self._reconcile_site(payload)
        except ElevenEnergyAuthError:
            return False
        except ElevenEnergyApiError as err:
            _LOGGER.warning("Eleven Energy Plus site poll failed: %s", err)
            return False
        return True

    async def _reconcile_site(self, payload: dict[str, Any]) -> None:
        """Create a :class:`HybridInverter` for each newly-listed inverter.

        Each new machine is read once before its entities are built: the
        site listing says what model it is, but how many batteries are fitted
        is only in the reading. A failed read does not stop discovery - the
        inverter is created from its listing and picks the rest up on the
        first successful poll. Devices whose ``type`` isn't
        ``"hybridinverter"`` (heat pumps, EV chargers, ...) are ignored.

        If the platforms have already set up, the new inverter's sensors and
        binary sensors are registered immediately; its poll-interval Number
        entity only appears after the next Home Assistant restart.
        """
        for summary in payload.get("devices", []):
            if not isinstance(summary, dict):
                continue
            device_id = summary.get("deviceId")
            if (
                not isinstance(device_id, str)
                or not device_id
                or summary.get("type") != "hybridinverter"
            ):
                continue
            if device_id in self.devices:
                continue

            _LOGGER.debug(
                "Eleven Energy Plus discovered inverter %s; raw site payload: %s",
                device_id,
                summary,
            )

            try:
                state: dict[str, Any] | None = await self._fetch_device(device_id)
            except ElevenEnergyApiError as err:
                _LOGGER.warning(
                    "Could not read Eleven Energy Plus device %s during "
                    "discovery: %s",
                    device_id,
                    err,
                )
                state = None

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

            serial = summary.get("serialNumber")
            inverter = HybridInverter(
                self.hass,
                self,
                device_id,
                label,
                serial if isinstance(serial, str) else "",
                model=model_block(summary, state),
                state=state,
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
            self.add_entities(
                "binary_sensor", inverter.binary_sensor_entities.values()
            )
            if state is not None:
                self._apply_device_payload(inverter, state)

    def _apply_device_payload(
        self, device: HybridInverter, payload: dict[str, Any]
    ) -> None:
        """Feed a successful device reading into its inverter."""
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
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Failed processing Eleven Energy Plus device %s update",
                device.device_id,
            )
        device.record_poll_success()

    async def poll_devices(self) -> None:
        """Poll every known device for fresh telemetry.

        Per-device failures are isolated: a failure for one device is logged,
        counted towards that device's availability, and the loop moves on.
        A rejected credential stops the cycle, since every further request
        would be rejected too.
        """
        for device in list(self.devices.values()):
            if self._shutdown.is_set():
                return
            try:
                payload = await self._fetch_device(device.device_id)
            except ElevenEnergyAuthError:
                return
            except ElevenEnergyApiError as err:
                _LOGGER.warning(
                    "Failed polling Eleven Energy Plus device %s: %s",
                    device.device_id,
                    err,
                )
                device.record_poll_failure()
                continue
            self._apply_device_payload(device, payload)

    def start_poller(self) -> None:
        """Start the background polling task.

        The poller does an immediate device poll so entities populate without
        waiting one full cadence, then loops on the poll-now event with a
        timeout equal to the current :attr:`poll_interval`. Each iteration
        polls the site (for newly-added devices) and then each device. All
        per-iteration errors are caught and logged so a transient API blip
        cannot kill the task; cancellation is not caught. While the
        credential is rejected the poller idles until the entry is reloaded.

        Registered via :meth:`ConfigEntry.async_create_background_task` so
        Home Assistant cancels it during a normal unload even if our own
        :meth:`terminate` is skipped.
        """

        async def periodic() -> None:
            try:
                await self.poll_devices()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Eleven Energy Plus initial device poll failed")

            while not self._shutdown.is_set():
                interval = self.poll_interval
                _LOGGER.debug(
                    "Eleven Energy Plus next poll in %ss (or sooner on request)",
                    interval,
                )
                try:
                    await asyncio.wait_for(self._poll_now.wait(), timeout=interval)
                except TimeoutError:
                    pass
                self._poll_now.clear()

                if self._shutdown.is_set():
                    return
                if self._auth_failed:
                    continue

                try:
                    await self.poll_site()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Eleven Energy Plus site poll failed")

                try:
                    await self.poll_devices()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Eleven Energy Plus device poll failed")

        self.poller_task = self.entry.async_create_background_task(
            self.hass, periodic(), "Eleven Energy Plus Poll"
        )

    async def terminate(self) -> None:
        """Stop the controller.

        Signals shutdown (which also aborts any POST backoff), cancels the
        poller task and waits up to 5 seconds for it to settle, so unloads
        and reloads don't leave a zombie poll racing against a fresh
        controller for the same entities.
        """
        self._terminated = True
        self._shutdown.set()
        self._poll_now.set()
        task = self.poller_task
        self.poller_task = None
        if task is None:
            return
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=5)
        if task in done and not task.cancelled() and task.exception() is not None:
            _LOGGER.error(
                "Eleven Energy Plus poll task raised during termination",
                exc_info=task.exception(),
            )
        _LOGGER.info("Eleven Energy Plus is no longer polling")
