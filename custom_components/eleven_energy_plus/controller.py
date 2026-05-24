"""The main Eleven Energy Plus coordinator.

The :class:`Controller` is the only thing in the integration that talks to
the Eleven Energy cloud API. It owns:

* The bearer token and the shared aiohttp session via
  :func:`async_get_clientsession`.
* The discovered ``HybridInverter`` devices and their predefined entities.
* The background polling task that periodically refreshes site + device
  data and feeds it through the inverter's payload walker.
* The dispatch path for the seven ``set_work_mode_*`` service calls,
  including input clamping, required-field validation, retry-with-backoff
  POST, and a stable structured response for automations.
* A small option-listener pub-sub used by the Number entity so it can
  rebroadcast cadence changes triggered via the OptionsFlow.

Everything here is async and cancellation-safe: the retry loop and the
poller cycle both ``await`` on a shared ``_wake_event`` so a shutdown or
options change can short-circuit any sleep almost immediately.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BASE_URL,
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    PLATFORMS,
)
from .hybrid_inverter import HybridInverter

_LOGGER = logging.getLogger(__name__)


# All outbound API calls share this timeout. ``total`` bounds the whole request
# including the response body; ``connect`` bounds the TCP/TLS handshake. Without
# these, a hung upstream would freeze the polling loop indefinitely.
API_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


# Caps on POST retries. With a doubling delay starting at 1s and a 5-attempt
# cap the worst case wait is 1+2+4+8 = 15s of sleeping plus 5 * total request
# timeout, which is bounded and cancellable via ``_wake_event``.
_MAX_POST_ATTEMPTS = 5

# Maximum length of the debug dump of the first device payload. Auto-discovered
# list fields can balloon the size of this log line.
_PAYLOAD_LOG_LIMIT = 2000


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


def _clamp_percent(value: Any) -> float | None:
    """Coerce a percent-like input into 0-100, returning None when uncoercible."""
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, coerced))


def _clamp_power_kw(value: Any) -> float | None:
    """Coerce a kW-like input into a 0-50 range, returning None when uncoercible."""
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(50.0, coerced))


def _clamp_minutes(value: Any) -> int | None:
    """Coerce a minutes-like input into a positive integer."""
    try:
        coerced = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(1, coerced)


def _coerce_bool(value: Any) -> bool:
    """Best-effort coercion to bool that handles "true"/"false" strings sent over YAML."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


@dataclass(frozen=True)
class PostResult:
    """Outcome of a ``send_reliable_post`` call."""

    status: int | None
    body: Any | None
    attempts: int


@dataclass
class _WorkModeRequest:
    """Sanitised user-facing parameters plus the API payload they translate to."""

    user_params: dict[str, Any] = field(default_factory=dict)
    api_params: dict[str, Any] = field(default_factory=dict)


class Controller:
    """Coordinator for a single Eleven Energy Plus config entry.

    A controller is constructed by :func:`async_setup_entry`, populated by an
    initial :meth:`poll_site` (which discovers the hybrid inverter devices),
    and then handed off to the platform setups. As each platform registers its
    ``async_add_entities`` callback via :meth:`complete_platform_setup`, the
    controller waits until all platforms in ``PLATFORMS`` have checked in
    before starting :meth:`start_poller`. From that point on the periodic
    poller drives the entity state and handles dynamic discovery.

    Lifecycle methods (in call order):

    * :meth:`initialise` - one-shot initial site poll, raises
      ``ConfigEntryNotReady`` on transient failure.
    * :meth:`complete_platform_setup` - called once per platform; the last
      one in starts the poller.
    * :meth:`set_work_mode` - service entry point; always returns a structured
      response dict, never raises.
    * :meth:`notify_options_changed` - called when the OptionsFlow updates a
      non-token option; wakes the poller and pings option listeners.
    * :meth:`terminate` - cancels the poller and waits up to 5s for cleanup.
    """

    def __init__(self, token: str, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the controller bound to a single config entry.

        :param token: API bearer token from the user's Eleven Energy site.
        :param hass: HA core object, used for the shared aiohttp session and
            the device/entity registries.
        :param entry: the ``ConfigEntry`` whose ``data['token']`` matches
            ``token``. Stored so ``poll_interval`` and the background task
            registration can reach it.
        """
        self.token = token
        self.hass = hass
        self.entry = entry
        self.poller_task: asyncio.Task | None = None
        self.headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        # Mapping of API device id -> HybridInverter wrapper. Populated by
        # ``poll_site`` and read by the platform setups, service dispatch and
        # the periodic poller.
        self.devices: dict[str, HybridInverter] = {}
        # ``async_add_entities`` callbacks indexed by platform name. Filled in
        # by ``complete_platform_setup`` as the platforms register.
        self._add_entities_callbacks: dict[str, AddEntitiesCallback] = {}
        # Subscribers to in-process option changes (currently just the Number
        # entity, which rebroadcasts its state when the cadence changes).
        self._option_listeners: list[Callable[[], None]] = []
        # Shared event for "poll now-ish" signalling: set by ``terminate``,
        # ``notify_options_changed`` and the success path of ``set_work_mode``,
        # awaited by both the poller cycle and the retry backoff.
        self._wake_event = asyncio.Event()
        self._terminated = False
        # Debug breadcrumb: we log the first device payload once per process
        # to help users (and us) understand the API's shape on their site.
        self._logged_first_payload = False
        # Snapshot of the device-label override active at construction time.
        # Used by ``_async_options_updated`` to decide whether the override
        # has actually changed, since :attr:`device_label_override` always
        # reads the current (possibly already-updated) entry options.
        self._configured_device_label = self.device_label_override

    @property
    def configured_device_label(self) -> str:
        """Return the device-label override snapshot from construction time.

        Compare new option values against this (not :attr:`device_label_override`)
        when deciding whether the override has changed - the live property
        reflects whatever is currently in ``entry.options``, which HA
        mutates in place before option-update listeners fire.
        """
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
        """Return the user-supplied device-card label, or ``""`` if unset.

        Strips whitespace and coerces non-string values defensively so the
        rest of the integration can rely on a clean, possibly-empty string
        without re-validating. An empty string means "no override".
        """
        raw = self.entry.options.get(CONF_DEVICE_LABEL, "")
        if not isinstance(raw, str):
            return ""
        return raw.strip()

    def register_platform(
        self, platform: str, callback: AddEntitiesCallback
    ) -> None:
        """Store a platform's async_add_entities so dynamic entities can be added later."""
        self._add_entities_callbacks[platform] = callback

    def add_entities(self, platform: str, entities: Iterable) -> None:
        """Add entities for a platform if its callback has been registered."""
        items = list(entities)
        if not items:
            return
        callback = self._add_entities_callbacks.get(platform)
        if callback is None:
            _LOGGER.debug(
                "No add-entities callback for platform %s; deferring %d entities",
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
        """Signal the poller that configuration options have changed."""
        self._wake_event.set()
        for listener in list(self._option_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Eleven Energy Plus option listener raised")

    async def send_reliable_post(
        self, url_suffix: str, payload: dict[str, Any]
    ) -> PostResult:
        """Make multiple attempts to post a request, doubling the delays each time.

        The retry loop is bounded by :data:`_MAX_POST_ATTEMPTS` and aborts early if
        the controller is being terminated, so unloads and reloads don't have to
        wait for a long backoff sleep.
        """
        session = async_get_clientsession(self.hass)
        last_status: int | None = None
        last_body: Any | None = None
        delay = 1
        attempts = 0

        while attempts < _MAX_POST_ATTEMPTS:
            if self._terminated:
                _LOGGER.info(
                    "Aborting Eleven Energy Plus POST to %s due to shutdown",
                    url_suffix,
                )
                break

            attempts += 1
            try:
                async with session.post(
                    f"{BASE_URL}{url_suffix}",
                    headers=self.headers,
                    json=payload,
                    timeout=API_TIMEOUT,
                ) as response:
                    last_status = response.status
                    try:
                        last_body = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        last_body = None
                    if response.status == 200:
                        return PostResult(
                            status=200, body=last_body, attempts=attempts
                        )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Eleven Energy Plus POST to %s failed", url_suffix
                )

            if attempts >= _MAX_POST_ATTEMPTS:
                break

            _LOGGER.info(
                "POST to %s got status %s (attempt %d/%d); retrying after %ds",
                url_suffix,
                last_status,
                attempts,
                _MAX_POST_ATTEMPTS,
                delay,
            )
            # Wait cancellably: ``_wake_event`` is set by ``terminate()``, so a
            # shutdown breaks us out of the backoff almost immediately.
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay *= 2

        return PostResult(status=last_status, body=last_body, attempts=attempts)

    def _sanitise_work_mode(
        self, mode: str, data: dict[str, Any]
    ) -> _WorkModeRequest:
        """Translate a service-call payload into clamped user and API parameters.

        Returns the user-facing dict (keys mirror services.yaml field names) and
        the API-facing dict (camelCase keys posted to the inverter), so the
        response we hand back to automations doesn't leak the API vocabulary.
        """
        request = _WorkModeRequest()

        def _store_percent(user_key: str, api_key: str) -> None:
            if user_key not in data:
                return
            clamped = _clamp_percent(data[user_key])
            if clamped is None:
                _LOGGER.warning(
                    "Eleven Energy Plus ignoring uncoercible %s=%r",
                    user_key,
                    data[user_key],
                )
                return
            request.user_params[user_key] = clamped
            request.api_params[api_key] = clamped

        def _store_power(user_key: str, api_key: str) -> None:
            if user_key not in data:
                return
            clamped = _clamp_power_kw(data[user_key])
            if clamped is None:
                _LOGGER.warning(
                    "Eleven Energy Plus ignoring uncoercible %s=%r",
                    user_key,
                    data[user_key],
                )
                return
            request.user_params[user_key] = clamped
            request.api_params[api_key] = clamped

        def _store_minutes(user_key: str, api_key: str) -> None:
            if user_key not in data:
                return
            clamped = _clamp_minutes(data[user_key])
            if clamped is None:
                _LOGGER.warning(
                    "Eleven Energy Plus ignoring uncoercible %s=%r",
                    user_key,
                    data[user_key],
                )
                return
            request.user_params[user_key] = clamped
            request.api_params[api_key] = clamped

        def _store_bool(user_key: str, api_key: str) -> None:
            if user_key not in data:
                return
            coerced = _coerce_bool(data[user_key])
            request.user_params[user_key] = coerced
            request.api_params[api_key] = coerced

        match mode:
            case "set_work_mode_self_consumption":
                _store_percent("percent_to_battery", "targetExcessPc")
            case "set_work_mode_force_charge":
                _store_percent("target_percent", "targetSoc")
                _store_power("target_power", "rate")
            case "set_work_mode_grid_export":
                _store_percent("target_percent", "targetSoc")
                _store_power("target_power", "rate")
                _store_bool("include_excess_solar", "addAverageExcess")
                _store_bool("overdrive", "overdrive")
            case "set_work_mode_pv_export":
                pass
            case "set_work_mode_idle_battery":
                _store_bool("allow_charging", "allowCharge")
                _store_bool("allow_discharging", "allowDischarge")
            case "set_work_mode_target_soc":
                _store_percent("target_soc", "targetSoc")
                _store_power("max_charge_power", "maxChargeRate")
                _store_power("max_discharge_power", "maxDischargeRate")
                _store_minutes("target_minutes", "targetMinutes")
            case "set_work_mode_reset":
                pass

        return request

    def _resolve_target_device(self, data: dict[str, Any]) -> str | None:
        """Resolve the Eleven Energy device id targeted by a service call.

        Resolution order:

        1. If the service call carries a ``device_id`` (HA's device registry
           id of the inverter device, supplied via the ``target.device``
           selector in ``services.yaml``), look it up in the registry and
           pull the Eleven Energy identifier out of its ``identifiers`` set,
           filtering on :data:`DOMAIN` so identifiers from other
           integrations on the same device entry are ignored.
        2. Otherwise (or if step 1 didn't yield a match), fall back to the
           first known ``hybridinverter`` device, which is the natural
           behaviour for single-inverter installs where the user hasn't
           bothered picking a device.

        Returns ``None`` when no inverter is known at all - the caller turns
        this into a structured error response.
        """
        if "device_id" in data:
            target = data["device_id"]
            hass_device_id = target[0] if isinstance(target, list) else target
            dev_reg = dr.async_get(self.hass)
            dev: DeviceEntry | None = dev_reg.async_get(hass_device_id)
            if dev is not None:
                for identifier in dev.identifiers:
                    if identifier[0] == DOMAIN:
                        return identifier[1]

        for device in self.devices.values():
            if device.type == "hybridinverter":
                return device.device_id
        return None

    async def set_work_mode(
        self, mode: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Change the inverter's work mode.

        Called by :func:`handle_set_workmode` for every
        ``eleven_energy.set_work_mode_*`` service. Always returns a response
        dict; **never raises**. Validation failures, missing devices and API
        errors are all surfaced via the ``error`` field.

        Response schema (stable, matches the table in ``README.md``)::

            {
                "success":   bool,                # True iff API returned 200.
                "status":    int | None,          # Last HTTP status, None if no request made.
                "device_id": str | None,          # Eleven Energy device id targeted.
                "work_mode": str | None,          # canonical API work mode posted.
                "params":    dict[str, Any],      # Sanitised user-facing parameters.
                "attempts":  int,                 # POST attempts made (1..5).
                "error":     str | None,          # Human-readable failure reason.
            }

        On success ``_wake_event`` is set so the poller picks up the new mode
        on the next iteration (sub-second on a fast site) rather than waiting
        a full ``poll_interval``.
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

        device_id = self._resolve_target_device(data)
        if device_id is None:
            _LOGGER.warning("Cannot perform set workmode as no device determined")
            result["error"] = "no Eleven Energy Plus device available"
            return result
        result["device_id"] = device_id

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
        result["params"] = request.user_params

        api_payload = {**request.api_params, "workMode": api_work_mode}

        post_result = await self.send_reliable_post(
            f"devices/{device_id}/operatingMode", api_payload
        )
        result["status"] = post_result.status
        result["attempts"] = post_result.attempts

        if post_result.status == 200:
            result["success"] = True
            # Wake the poller so the new mode is reflected promptly.
            self._wake_event.set()
        else:
            result["error"] = f"API returned status {post_result.status}"
            _LOGGER.warning(
                "Unable to change work mode, last status was %s", post_result.status
            )

        return result

    async def initialise(self) -> None:
        """Set up the controller.

        Raises :class:`ConfigEntryNotReady` when the initial site poll fails or
        returns no hybrid inverter devices, so Home Assistant's standard
        config-entry retry/backoff kicks in instead of leaving an empty,
        zero-entity integration sitting in the registry.
        """
        _LOGGER.info("Eleven Energy Plus initialising")
        succeeded = await self.poll_site()
        if not succeeded:
            raise ConfigEntryNotReady(
                "Eleven Energy site API was unreachable during setup"
            )
        if not self.devices:
            raise ConfigEntryNotReady(
                "Eleven Energy site returned no hybrid inverter devices"
            )

    def start_poller(self) -> None:
        """Start the background polling task.

        The poller does an immediate device poll so entities populate without
        waiting one full cadence, then loops on ``_wake_event`` with a timeout
        equal to the current :attr:`poll_interval`. On every iteration it
        polls the site (for newly-added devices) and then each device. All
        per-iteration errors are caught and logged so a transient API blip
        cannot kill the task.

        Registered via :meth:`ConfigEntry.async_create_background_task` so
        Home Assistant cancels it during a normal unload even if our own
        :meth:`terminate` is skipped.
        """

        async def periodic() -> None:
            # Run an immediate device poll so entities populate without waiting a cycle.
            try:
                await self.poll_devices()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Eleven Energy Plus initial device poll failed")

            while not self._terminated:
                interval = self.poll_interval
                _LOGGER.debug(
                    "Eleven Energy Plus next poll in %ss (or sooner on options change)",
                    interval,
                )
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                self._wake_event.clear()

                if self._terminated:
                    return

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

    async def poll_devices(self) -> None:
        """Poll every known device for fresh telemetry.

        Per-device failures are isolated: an HTTP exception or non-200 status
        for one device is logged and the loop moves on to the next, so a
        single failing inverter in a multi-inverter site does not stop the
        rest of the system from updating. The first successful payload of
        the process is dumped to DEBUG (truncated to
        :data:`_PAYLOAD_LOG_LIMIT` chars) to help with troubleshooting.
        """
        session = async_get_clientsession(self.hass)
        for device in list(self.devices.values()):
            try:
                async with session.get(
                    f"{BASE_URL}devices/{device.device_id}",
                    headers=self.headers,
                    timeout=API_TIMEOUT,
                ) as response:
                    if response.status != 200:
                        _LOGGER.warning(
                            "Eleven Energy Plus device %s API responded with %s",
                            device.device_id,
                            response.status,
                        )
                        continue
                    payload = await response.json()
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Failed polling Eleven Energy Plus device %s",
                    device.device_id,
                )
                continue

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

    async def poll_site(self) -> bool:
        """Poll the site endpoint and reconcile known devices.

        Used both for initial device discovery during :meth:`initialise` and
        on every poller cycle so newly-added inverters get picked up without
        a Home Assistant restart. Devices whose ``type`` isn't
        ``"hybridinverter"`` are ignored - they are exposed by the site API
        but currently unsupported (heat pumps, EV chargers, etc.).

        For each newly-seen hybrid inverter a :class:`HybridInverter` is
        constructed (predefined entities and all), parked in
        :attr:`devices`, and - if the platforms have already initialised -
        registered with HA via the cached add-entities callbacks. The
        per-device poll-interval Number entity is only created during the
        initial platform setup, so a brand new inverter discovered at
        runtime won't get one until the next Home Assistant restart.

        Returns ``True`` only when the API responded with a 200 and a
        parseable JSON body; callers like :meth:`initialise` use that to
        decide whether to raise :class:`ConfigEntryNotReady`.
        """
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{BASE_URL}site", headers=self.headers, timeout=API_TIMEOUT
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(
                        "Eleven Energy Plus call to site API responded with %s",
                        response.status,
                    )
                    return False
                payload = await response.json()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Eleven Energy Plus site poll failed")
            return False

        for device in payload.get("devices", []):
            device_id = device.get("deviceId")
            device_type = device.get("type")
            if device_id is None or device_type != "hybridinverter":
                continue
            if device_id in self.devices:
                continue

            # Log the raw site-payload entry for this device at INFO so users
            # who suspect the upstream API is mislabelling their hardware can
            # verify what was actually returned without first having to enable
            # debug logging. Issued once per device (newly-discovered devices
            # short-circuit on ``device_id in self.devices`` above).
            _LOGGER.info(
                "Eleven Energy Plus discovered inverter %s; raw site payload: %s",
                device_id,
                device,
            )

            # Pick the most informative product-label field the API exposes.
            # ``name`` is the historically-used field and remains the primary
            # source so existing installations see no behaviour change.
            # ``model`` / ``productName`` / ``inverterModel`` are tried in
            # order only as fall-backs, so an Eleven Energy account whose
            # ``name`` is empty/null still gets a sensible device label if
            # the API carries the product info elsewhere.
            #
            # If ``name`` is populated but mislabelled (e.g. a stale
            # provisioning default like "North Sea 6" against actual
            # Mediterranean Sea 12 hardware) we still pass it through here:
            # that is an upstream data issue and changing field priority
            # silently would surprise users whose ``name`` is correct.
            # The raw payload logged just above lets affected users
            # confirm what the API returned, and the user-supplied
            # ``device_label_override`` (read from options just below) lets
            # them pin the displayed label without waiting on Eleven Energy
            # support.
            device_name = (
                device.get("name")
                or device.get("model")
                or device.get("productName")
                or device.get("inverterModel")
                or "Inverter"
            )
            # Apply the optional user override last so it wins over every
            # API-derived choice above. Stored as a stripped string by the
            # OptionsFlow; treat anything falsy (empty string, missing key)
            # as "no override".
            override = self.device_label_override
            if override:
                _LOGGER.info(
                    "Eleven Energy Plus applying device label override for %s: "
                    "%r (API said %r)",
                    device_id,
                    override,
                    device_name,
                )
                device_name = override
            # Coerce ``null`` API values with ``or`` rather than ``dict.get``
            # defaults, since ``dict.get`` only falls back when the key is
            # missing entirely. The "Inverter" tail yields a sensible
            # composite device card name like "Eleven Energy Plus Inverter"
            # rather than the brand-doubled "Eleven Energy Plus Eleven Energy".
            inverter = HybridInverter(
                self.hass,
                self,
                device_id,
                device_name,
                device.get("serialNumber") or "",
            )
            self.devices[device_id] = inverter
            _LOGGER.info(
                "Created inverter %s (label=%r)", device_id, device_name
            )

            # If platforms have already initialised, register the new device's
            # static entities immediately. The poll-interval Number entity is only
            # added during platform setup; newly-discovered inverters will get one
            # on the next Home Assistant restart.
            self.add_entities("sensor", inverter.sensor_entities.values())
            self.add_entities(
                "binary_sensor", inverter.binary_sensor_entities.values()
            )

        return True

    def complete_platform_setup(
        self, platform: str, callback: AddEntitiesCallback
    ) -> None:
        """Record a platform's add-entities callback and maybe start polling.

        Every platform under :data:`PLATFORMS` calls this from its
        ``async_setup_entry``. We deliberately defer starting the poller
        until *all* expected platforms have registered: that way the first
        ``poll_devices`` call can hand newly-discovered dynamic entities to
        every platform's callback without dropping any. After that gate is
        crossed the poller is started exactly once per controller lifetime.
        """
        self.register_platform(platform, callback)

        expected = {p.value for p in PLATFORMS}
        if (
            expected.issubset(self._add_entities_callbacks.keys())
            and self.poller_task is None
            and not self._terminated
        ):
            self.start_poller()

    async def terminate(self) -> None:
        """End the controller.

        Cancels the poller task and waits up to 5 seconds for it to settle, so
        unloads and reloads don't leave a zombie poll racing against a fresh
        controller for the same entities.
        """
        self._terminated = True
        self._wake_event.set()
        task = self.poller_task
        self.poller_task = None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Eleven Energy Plus poll task raised during termination"
            )
        _LOGGER.info("Eleven Energy Plus is no longer polling")
