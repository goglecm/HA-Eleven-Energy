# Changelog

All notable changes to the Eleven Energy Plus Home Assistant integration are
captured here. Versions correspond to the `version` field in
`custom_components/eleven_energy_plus/manifest.json`, and the format roughly
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 1.5.0 - 2026-05-24

### Breaking

- **Integration renamed to Eleven Energy Plus.** The HA integration domain
  is now `eleven_energy_plus` (was `eleven_energy`), the filesystem path is
  now `custom_components/eleven_energy_plus/`, and the HACS display name
  is "Eleven Energy Plus". This is what lets the fork install side-by-side
  with the original, unmaintained
  [`iPeel/HA-Eleven-Energy`](https://github.com/iPeel/HA-Eleven-Energy)
  HACS repository without colliding on filesystem path, registry
  namespace or service prefixes.
- **Service prefix change.** All work-mode services are now reachable as
  `eleven_energy_plus.set_work_mode_*` (was `eleven_energy.set_work_mode_*`).
  Existing automations targeting the original integration will not carry
  over and must be updated.
- **Config entry / device card renames.** New config entries are titled
  "Eleven Energy Plus" and inverter device cards read
  "Eleven Energy Plus {device_name}" (was "Eleven Energy {device_name}").
  The hardware `manufacturer` remains "Eleven Energy" - that hasn't
  changed.
- **No registry migration is performed.** Users moving from the original
  integration need to remove it, install Eleven Energy Plus, re-add the
  API token, and rewrite any automations / dashboards. Coexistence is
  supported but no automatic data transfer.

### Fixed

- Null `name` from the site API previously produced a brand-doubled
  device card ("Eleven Energy Eleven Energy"). The fallback is now the
  generic placeholder "Inverter", yielding the sensible
  "Eleven Energy Plus Inverter".
- Dropped the no-longer-reachable 1.2->1.3 legacy entity migration
  helper; under the new domain there is nothing to migrate from.

### Changed

- `manifest.json` version bumped to 1.5.0.
- Log messages, error strings and the background task name now use the
  "Eleven Energy Plus" brand so they're easy to grep for separately
  from the original integration's output.
- `services.yaml` `target.device.integration` switched to
  `eleven_energy_plus`, so device-targeted service calls filter the
  device picker to the fork's devices.

## 1.4.0 - 2026-05-24

### Added

- **Structured service responses.** Every `set_work_mode_*` service now
  declares `supports_response: optional` and returns a stable dict
  (`success`, `status`, `device_id`, `work_mode`, `params`, `attempts`,
  `error`) so automations can branch on the outcome. The full schema is
  documented in the README and on `Controller.set_work_mode`.
- **Firmware version sensor.** The inverter's `firmwareVersion` is now
  exposed as a diagnostic `_firmware_version` sensor.
- **Expanded auto-discovery heuristics.** Reactive power (`var`), apparent
  power (`VA`), lifetime/total energy counters, battery charge/discharge
  rate keywords, state-of-health, and version-like strings are now
  recognised by name and given correct units, device classes and icons.
- **List-valued field expansion.** Lists in the device payload (e.g.
  `pvStrings`) are walked with index-based paths
  (`pvStrings.0.power`, `pvStrings.1.power`, ...), capped at 32 entries per
  list with a one-shot truncation warning.
- **Test suite.** A pytest-based test suite using
  `pytest-homeassistant-custom-component` covers the config flow,
  controller HTTP / retry / termination behaviour, payload walking,
  leaf-failure isolation, dynamic discovery, and service-response wiring.
  Run with `.venv/bin/pytest tests/`.

### Changed

- **Service targeting.** Every service in `services.yaml` now uses
  `target: { device: { integration: eleven_energy } }` so device-targeted
  service calls route correctly on multi-inverter sites.
- **Bounded, cancellable retries.** `Controller.send_reliable_post` is
  capped at five attempts and uses a cancellable wake-event-based backoff,
  so unloads and reloads no longer have to wait out long retry sleeps.
- **HTTP timeouts everywhere.** All outbound GET/POST calls now use a 30
  s total / 10 s connect timeout (15 s / 10 s in the config flow).
- **Setup rollback.** Failures during `async_setup_entry`'s platform
  forwarding now terminate the in-flight controller and remove it from
  `hass.data`, so a retry starts from a clean slate.
- **Per-leaf error isolation.** A single malformed field can no longer
  poison sibling entities; both branch recursions and leaf writes are
  wrapped in try/except with focused warnings.
- **Input validation.** Required service fields are checked before any
  HTTP traffic, numeric inputs are clamped to safe ranges
  (`0-100 %`, `0-50 kW`, `>= 1 min`), and boolean inputs accept the usual
  YAML truthy strings.
- **OptionsFlow modernisation.** Dropped the deprecated explicit
  `OptionsFlowHandler.__init__` assignment, replaced the YAML
  `CONFIG_SCHEMA` with `cv.config_entry_only_config_schema(DOMAIN)`, and
  distinguished 401/403 (`invalid_auth`) from other failures
  (`cannot_connect`) in token validation.
- **Manifest and branding** updated to point at
  [`goglecm/HA-Eleven-Energy`](https://github.com/goglecm/HA-Eleven-Energy).

### Fixed

- `dict.get(key, default)` returning `None` for explicit `null` API
  values now falls back correctly via the `value or default` idiom for
  device name and serial number.
- `services.yaml` typo "to export toi the grid".
- `services.yaml` field name `percentage_to_battery` renamed to
  `percent_to_battery` to match the controller.
- The `already_setup` translation string no longer references unrelated
  IP/CSV config; it now explains the single-site constraint.

## 1.3.0 - 2026-05-24

### Added

- **Configurable poll interval.** The integration now polls every 15
  seconds by default (was 60), with the cadence settable from 5 to 300
  seconds in 5-second steps via either:
  - the per-inverter `number.{device_id}_poll_interval` entity, or
  - **Settings -> Devices & Services -> Eleven Energy -> Configure**.
- **Dynamic entity discovery.** Anything the API returns that isn't on
  the curated list is auto-exposed as a diagnostic sensor or binary
  sensor with heuristically-inferred units and device classes.
- **Service definition for `set_work_mode_target_soc`.** Previously the
  controller dispatched it but `services.yaml` did not declare it.
- **Legacy entity migration.** On first start after upgrade the
  integration removes the mis-domained `sensor.*_system_online` entity
  from the registry; the new entity is the correctly-typed
  `binary_sensor.*_system_online`.

### Changed

- Controller, polling and platform setup hardened against transient API
  failures (per-device error isolation, immediate-then-periodic polling,
  proper shutdown handling).
- `Controller.terminate()` is now async and waits up to 5 seconds for the
  poller task to settle, preventing zombie tasks racing a fresh
  controller after a reload.
- `aiohttp.ClientSession` is sourced from
  `async_get_clientsession(hass)` instead of being constructed and never
  closed.
- Config flow uses `@staticmethod @callback async_get_options_flow` per
  Home Assistant's current pattern.

### Fixed

- `InverterBinarySensorEntity` registered its `entity_id` under the
  `sensor` platform; it is now correctly under `binary_sensor`.
- Various `_LOGGER(...)` (callable) typos replaced with
  `_LOGGER.warning(...)` etc.
- `SensorStateClass.TOTAL` was paired with `_attr_last_reset` in a way
  that emitted runtime warnings; both have been removed in favour of
  `TOTAL_INCREASING`.

## Earlier releases

The integration was originally created and maintained by
[@iPeel](https://github.com/iPeel). Releases prior to 1.3 predate this
fork and are not documented here.
