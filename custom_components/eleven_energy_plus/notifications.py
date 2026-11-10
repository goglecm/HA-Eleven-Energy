"""Repair issues and persistent notifications for lasting faults.

A fault that lasts, or that needs the user to act, is reported in two
places: a repair issue (Settings > Repairs), which stays for as long as the
fault does, and - the first time it appears - a persistent notification, so
it is seen without going looking. Both go away when the fault does.

* ``cloud_unreachable``: no request has been answered for
  :data:`~.const.CLOUD_UNREACHABLE_AFTER_SECONDS` (a rejected credential
  and rate limiting are answers).
* ``inverter_offline_<id>``: the inverter has reported ``online: false``
  for :data:`~.const.INVERTER_OFFLINE_AFTER_SECONDS` (the timer restarts
  after the inverter has been missing from the listing).
* ``inverter_missing_<id>``: the inverter has left the site listing; its
  device can then be deleted from the device page.
* ``work_mode_failed_<id>``: the last work-mode command for the inverter
  failed for a reason other than invalid input. Cleared by the next success.
* ``optimiser_off_<id>``: a work mode was set from Home Assistant and no
  Reset Work Mode has followed for :data:`~.const.OPTIMISER_OFF_AFTER`.
  The time it was set is persisted, so the measurement survives a restart.
* a rejected credential: Home Assistant's own reauthentication prompt, plus
  a persistent notification.

Durations are measured with the monotonic clock, except the optimiser timer,
which must survive a restart and so uses UTC wall-clock time; a wall clock
that moves backwards restarts it rather than producing a negative age.
"""

from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    CLOUD_UNREACHABLE_AFTER_SECONDS,
    DOMAIN,
    INVERTER_OFFLINE_AFTER_SECONDS,
    OPTIMISER_OFF_AFTER,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# The work mode that hands control back to Eleven Energy's optimiser.
RESET_SERVICE = "set_work_mode_reset"
CREDENTIAL_NOTIFICATION = "credential_rejected"

_SEVERITY_LOG_LEVEL = {
    ir.IssueSeverity.ERROR: logging.ERROR,
    ir.IssueSeverity.WARNING: logging.WARNING,
}


def _storage_key(entry: ConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


class _Placeholders(dict[str, str]):
    """Format mapping that leaves an unknown ``{placeholder}`` as written."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


class FaultReporter:
    """Raise and clear this entry's repair issues and notifications."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Create the reporter; :meth:`async_load` reads translations and state."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, _storage_key(entry)
        )
        self._texts: dict[str, str] = {}
        # Issue ids currently raised.
        self._active: set[str] = set()
        # Condition key -> monotonic time it was first seen, for the
        # conditions that only become an issue once they have lasted.
        self._since: dict[str, float] = {}
        # Inverter device id -> when a work mode other than Reset was set.
        self._optimiser_off_since: dict[str, datetime] = {}
        self._credential_notified = False
        # Set by :meth:`async_unload`: nothing would be left to clear what a
        # late report (a command finishing after unload) raised.
        self._closed = False

    @property
    def active_issues(self) -> list[str]:
        """Return the ids of the issues currently raised (for diagnostics)."""
        return sorted(self._active)

    @property
    def optimiser_off_since(self) -> dict[str, str]:
        """Return when each inverter's optimiser was switched off, ISO 8601 UTC."""
        return {
            device_id: when.isoformat()
            for device_id, when in self._optimiser_off_since.items()
        }

    async def async_load(self) -> None:
        """Load the notification texts and the persisted optimiser timers."""
        self._texts = await async_get_translations(
            self.hass, self.hass.config.language, "issues", [DOMAIN]
        )
        stored = await self._store.async_load()
        timers = stored.get("optimiser_off_since") if isinstance(stored, dict) else None
        if not isinstance(timers, dict):
            return
        for device_id, raw in timers.items():
            when = dt_util.parse_datetime(raw) if isinstance(raw, str) else None
            if when is None or when.tzinfo is None:
                _LOGGER.warning(
                    "Eleven Energy Plus discarded an unreadable optimiser timer "
                    "for %s: %r",
                    device_id,
                    raw,
                )
                continue
            self._optimiser_off_since[str(device_id)] = dt_util.as_utc(when)

    # ------------------------------------------------------------------
    # Issues and notifications
    # ------------------------------------------------------------------

    def _text(self, translation_key: str, part: str, placeholders: dict[str, str]) -> str:
        template = self._texts.get(
            f"component.{DOMAIN}.issues.{translation_key}.{part}", translation_key
        )
        return template.format_map(_Placeholders(placeholders))

    @staticmethod
    def _notification_id(issue_id: str) -> str:
        return f"{DOMAIN}_{issue_id}"

    @callback
    def raise_issue(
        self,
        issue_id: str,
        translation_key: str,
        severity: ir.IssueSeverity,
        placeholders: dict[str, str],
        *,
        refresh: bool = False,
    ) -> None:
        """Raise an issue and, as it appears, a persistent notification.

        An issue already raised is left alone, or with ``refresh`` has its
        text updated (the notification is not shown again). Once unloaded,
        nothing is raised.
        """
        already_active = issue_id in self._active
        if self._closed or (already_active and not refresh):
            return
        self._active.add(issue_id)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders=placeholders,
        )
        if already_active:
            return
        title = self._text(translation_key, "title", placeholders)
        persistent_notification.async_create(
            self.hass,
            self._text(translation_key, "description", placeholders),
            title=title,
            notification_id=self._notification_id(issue_id),
        )
        _LOGGER.log(_SEVERITY_LOG_LEVEL[severity], "Eleven Energy Plus: %s", title)

    @callback
    def clear_issue(self, issue_id: str) -> None:
        """Delete an issue and dismiss its notification, if raised."""
        if issue_id not in self._active:
            return
        self._active.discard(issue_id)
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        persistent_notification.async_dismiss(self.hass, self._notification_id(issue_id))
        _LOGGER.info("Eleven Energy Plus: resolved %s", issue_id)

    @callback
    def _lasting(
        self,
        issue_id: str,
        present: bool,
        threshold_seconds: float,
        translation_key: str,
        severity: ir.IssueSeverity,
        placeholders: dict[str, str],
    ) -> None:
        """Raise ``issue_id`` once the condition has lasted ``threshold_seconds``."""
        if not present:
            self._since.pop(issue_id, None)
            self.clear_issue(issue_id)
            return
        now = time.monotonic()
        since = self._since.setdefault(issue_id, now)
        if now - since >= threshold_seconds:
            self.raise_issue(issue_id, translation_key, severity, placeholders)

    # ------------------------------------------------------------------
    # The faults
    # ------------------------------------------------------------------

    @callback
    def report_poll_cycle(self, reached_api: bool) -> None:
        """Record whether a poll cycle got any answer from the API."""
        self._lasting(
            "cloud_unreachable",
            not reached_api,
            CLOUD_UNREACHABLE_AFTER_SECONDS,
            "cloud_unreachable",
            ir.IssueSeverity.ERROR,
            {"minutes": str(CLOUD_UNREACHABLE_AFTER_SECONDS // 60)},
        )

    @callback
    def report_offline(self, device_id: str, name: str, offline: bool) -> None:
        """Record whether an inverter reports itself offline."""
        self._lasting(
            f"inverter_offline_{device_id}",
            offline,
            INVERTER_OFFLINE_AFTER_SECONDS,
            "inverter_offline",
            ir.IssueSeverity.WARNING,
            {"name": name, "minutes": str(INVERTER_OFFLINE_AFTER_SECONDS // 60)},
        )

    @callback
    def report_missing(self, device_id: str, name: str, missing: bool) -> None:
        """Record whether an inverter has left the site listing."""
        issue_id = f"inverter_missing_{device_id}"
        if missing:
            self.raise_issue(
                issue_id, "inverter_missing", ir.IssueSeverity.WARNING, {"name": name}
            )
        else:
            self.clear_issue(issue_id)

    @callback
    def report_work_mode_result(
        self, device_id: str, name: str, error: str | None
    ) -> None:
        """Record the outcome of a work-mode command (``error`` None = success)."""
        issue_id = f"work_mode_failed_{device_id}"
        if error is None:
            self.clear_issue(issue_id)
            return
        self.raise_issue(
            issue_id,
            "work_mode_failed",
            ir.IssueSeverity.ERROR,
            {"name": name, "error": error},
            refresh=True,
        )

    @callback
    def record_work_mode(self, device_id: str, service: str) -> None:
        """Start or stop the optimiser timer after a successful command."""
        if self._closed:
            return
        if service == RESET_SERVICE:
            if self._optimiser_off_since.pop(device_id, None) is not None:
                self._save()
            self.clear_issue(f"optimiser_off_{device_id}")
            return
        if device_id not in self._optimiser_off_since:
            self._optimiser_off_since[device_id] = dt_util.utcnow()
            self._save()

    @callback
    def check_optimiser(self, names: dict[str, str]) -> None:
        """Raise ``optimiser_off`` for every timer older than the threshold.

        ``names`` maps the inverters still known to their display names; a
        timer for an inverter that is no longer known is dropped.
        """
        now = dt_util.utcnow()
        changed = False
        for device_id, since in list(self._optimiser_off_since.items()):
            if device_id not in names:
                del self._optimiser_off_since[device_id]
                self.clear_issue(f"optimiser_off_{device_id}")
                changed = True
                continue
            if now < since:
                _LOGGER.warning(
                    "Eleven Energy Plus: the clock moved back past the time the "
                    "optimiser was switched off on %s; restarting its timer",
                    names[device_id],
                )
                self._optimiser_off_since[device_id] = now
                changed = True
                continue
            if now - since >= OPTIMISER_OFF_AFTER:
                self.raise_issue(
                    f"optimiser_off_{device_id}",
                    "optimiser_off",
                    ir.IssueSeverity.WARNING,
                    {
                        "name": names[device_id],
                        "hours": str(int(OPTIMISER_OFF_AFTER.total_seconds() // 3600)),
                    },
                )
        if changed:
            self._save()

    @callback
    def report_credential(self, rejected: bool) -> None:
        """Show or withdraw the rejected-credential notification."""
        notification_id = self._notification_id(CREDENTIAL_NOTIFICATION)
        if rejected:
            if self._closed or self._credential_notified:
                return
            self._credential_notified = True
            persistent_notification.async_create(
                self.hass,
                self._text(CREDENTIAL_NOTIFICATION, "description", {}),
                title=self._text(CREDENTIAL_NOTIFICATION, "title", {}),
                notification_id=notification_id,
            )
            return
        if self._credential_notified:
            self._credential_notified = False
            persistent_notification.async_dismiss(self.hass, notification_id)

    @callback
    def forget_device(self, device_id: str) -> None:
        """Drop everything reported about an inverter that was deleted."""
        for prefix in ("inverter_offline_", "inverter_missing_", "work_mode_failed_", "optimiser_off_"):
            self._since.pop(f"{prefix}{device_id}", None)
            self.clear_issue(f"{prefix}{device_id}")
        if self._optimiser_off_since.pop(device_id, None) is not None:
            self._save()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _data(self) -> dict[str, Any]:
        return {"optimiser_off_since": self.optimiser_off_since}

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data, 1)

    async def async_unload(self) -> None:
        """Withdraw everything shown and write the optimiser timers now.

        Nothing is monitored once the entry is unloaded, so nothing it showed
        can be trusted to be cleared later, and nothing reported afterwards
        is shown or stored: the timers survive in storage, which from then on
        belongs to the entry's next reporter.
        """
        self._closed = True
        for issue_id in list(self._active):
            self.clear_issue(issue_id)
        self._since.clear()
        self.report_credential(False)
        await self._store.async_save(self._data())

    @staticmethod
    async def async_remove(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Delete everything stored or shown for a removed entry."""
        await Store[dict[str, Any]](hass, STORAGE_VERSION, _storage_key(entry)).async_remove()
        registry = ir.async_get(hass)
        for domain, issue_id in list(registry.issues):
            if domain == DOMAIN:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
