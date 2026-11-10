# Changelog

All notable changes to the Eleven Energy Plus Home Assistant integration are
captured here. Versions correspond to the `version` field in
`custom_components/eleven_energy_plus/manifest.json`, and the format roughly
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

A review of the parts the 2.0.0 audit did not harden - the lifecycle, the
flows, diagnostics, sign-in and every point where two things can overlap -
found eight more defects (`D35`-`D42` in
[`tests/DEFECTS.md`](tests/DEFECTS.md)), each fixed test-first with its tests
proven to fail on the commit that had it.

### Fixed

- **A device id could address another device** (D42). Ids from the site
  listing were used unencoded in request paths, so an id such as `x/../b`
  would have read, and sent work modes to, inverter `b`. Ids are now encoded
  as a single path segment, and `.` / `..` are ignored with a warning.
- **Two sign-ins open at once spoiled each other** (D35): they shared one
  PKCE verifier, so the first to finish (for example a reauthentication
  started beside a Reconfigure) failed. Each flow now has its own.
- **An inverter back in the site listing showed its old values** (D36) until
  three polls had failed; it now stays unavailable until it is read again.
- **Reports after unload or reload** (D37): a command still being sent when
  the integration unloaded raised a repair issue nothing would clear, and
  after a reload the stopped controller could save optimiser timers over the
  running one's. Its outcome now goes to the controller running the entry,
  or nowhere.
- **A command to an inverter deleted meanwhile** raised an issue for it
  (D38); a reading arriving after deletion is dropped too.
- **Outage timers counted unobserved time** (D40): the cloud-unreachable
  timer ran on through a rejected credential, which is an answer, and an
  inverter's offline timer through the time it was missing from the
  listing, so either could raise its repair the moment monitoring resumed.
- After a failed unload, **diagnostics reported the stopped controller as
  loaded** (D39) and **devices could not be deleted** although nothing polls
  the site (D41).

### Documentation

- New [CONTRIBUTING.md](CONTRIBUTING.md) (setup, the three Home Assistant
  versions, running tests and coverage, when and how to add tests, the
  defect protocol, static gates, mutation testing, CI, releasing) and
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (modules, lifecycle, polling,
  values, commands, fault reporting, the concurrency model and its
  invariants, known limitations). The README is for users and points to
  both.
- `tests/tools/check_red.py` proves each ledger entry against the commit
  that had the defect (`fails at <sha>` in the ledger).

## 2.0.0 - 2026-11-09

Brings the integration up to Home Assistant 2026.11 (Marketplace, hassfest
rules, DeviceInfo dataclass, unit enums, entity lifecycle), ports the useful
work from the 28 commits upstream (`iPeel/HA-Eleven-Energy` 2.0 - 2.2, up to
`78de8d1`; see [UPSTREAM.md](UPSTREAM.md)), hardens every path that can
control the inverter, and closes the 34 defects of a test-first audit
(`D01`-`D34`, each with its failing-then-passing tests in
[`tests/DEFECTS.md`](tests/DEFECTS.md)). Verified on Home Assistant 2026.3.4,
2026.10.4 and 2026.11.1; hassfest (HA dev) is clean.

### Hardened (fault-intolerant audit)

Values and availability:

- A field that becomes null or disappears from a reading reads unknown
  instead of keeping its last value for ever (D11).
- While the inverter reports `online: false`, its telemetry is unavailable;
  Online, Status, Firmware Version, API Connection and the ratings stay
  available (D12).
- Energy counters (`total_increasing`) never report a false meter reset:
  a lower reading is held as unknown until a second one confirms a real
  reset (at least a 10 % drop, as at midnight); a small correction holds
  the previous value; negatives are never shown (D13). The judgement uses
  the value from before a restart, and the poller no longer re-reads at
  start-up what discovery has just read, which would have confirmed a
  glitch (D33).
- MPPT and battery items with no usable index, a repeated index or a
  tracker index the model does not have are ignored and reported once
  (log and diagnostics) instead of silently (D14).
- A reading that fails to process counts as a failed poll rather than a
  success (D02); an inverter missing from two site listings goes
  unavailable and can be deleted (D04); shutting down, even through a
  failed unload, makes every entity unavailable first (D10).
- An inverter found at runtime gets its Update Interval entity straight
  away (D03).

Credentials and flows:

- An API token rejected by mistake recovers on its own: it is re-tested
  every 10 minutes, and on success polling resumes and the reauthentication
  prompt is withdrawn (D01).
- Every credential - setup, Reconfigure and reauthentication, token or
  sign-in - is checked with the API before it is stored: a site with no
  hybrid inverter is refused (D18), so is a credential for a different site
  than the existing entry's (D19), and a sign-in is now probed like a token
  (D20).
- The device-label override is limited to 64 printable characters (D21);
  the options form refuses a NaN or fractional interval instead of silently
  saving the default or truncating it (D32).

Actions and polling:

- `Retry-After` is understood as an HTTP date as well as seconds, and a
  rate-limited poll defers the next one (D05).
- Ambiguous booleans are refused everywhere - the controller no longer
  guesses, and the action schema no longer accepts any number (`2`) as
  true (D06). A stored poll interval is parsed strictly and an unusable one
  is reported once; infinity no longer crashes the poller (D07). The Update
  Interval entity refuses NaN instead of crashing (D30).
- Each failure kind raises its own error and message, and is reported as
  `error_kind` in the response: a superseded command is no longer called
  an invalid request (D08), a rejected credential and a shutdown are no
  longer "failed" (D16), and an unexpected error is contained, logged and
  reported as `internal_error` instead of escaping raw (D09).

Fault reporting (new):

- Repair issues, each with a one-time notification, for an unreachable
  cloud (15 minutes), an offline inverter (15 minutes), an inverter no
  longer on the site, a failed work-mode command, and the optimiser left
  off for 24 hours after a work mode (persisted across restarts; a clock
  moving backwards restarts it). A rejected credential adds a notification
  to Home Assistant's reauthentication prompt. All are withdrawn when the
  fault clears and removed with the entry.
- **API Connection** diagnostic binary sensor per inverter: whether the last
  attempt to read it from the cloud worked. It stays available when
  everything else is not.

Diagnostics and housekeeping:

- Diagnostics redact address, postcode, coordinates, email and phone (D23),
  log a failed version lookup instead of swallowing it (D31), have the same
  shape with or without a loaded entry (D34), and report the active issues,
  offline / missing state, last successful poll and ignored list items.
- `SERVICE_NAMES` is no longer re-exported for tests (D22); removing the
  entry deletes its stored state and issues.
- Typing is strict-clean on Home Assistant 2026.3, 2026.10 and 2026.11 (D15,
  D17, D25, D28); unused translation keys removed (D26); lint clean (D27);
  the test guard now also fails on Home Assistant's "not strictly
  increasing" and negative-counter warnings (D29); the PKCE challenge is
  proven to match the verifier (D24).
- The percent unit is the plain `"%"` string on every version, which removed
  the version shim.
- Test suite: one test file per module, 100 % line and branch coverage with
  no `pragma: no cover` (only `if TYPE_CHECKING:` blocks are excluded), a
  functional-coverage model with a gate, defect sweeps
  re-proved red against the pre-audit code (`tests/tools/check_red.py`),
  hypothesis fuzzing, mutation testing, vulture.

### Breaking

- **Work-mode inputs are validated, not clamped.** Every action has a schema:
  out-of-range values, `nan` / infinity, booleans or text where a number is
  expected, ambiguous booleans, unknown fields and missing required fields
  now fail the call with a validation error before anything is sent.
  Previously they were clamped (`150` % became `100` %), dropped, or - for
  `"nan"` - turned into the maximum (see Fixed). Limits: percentages 0-100,
  powers 0-50 kW (the API's ceiling; the UI slider stops at 20 kW),
  `target_minutes` whole minutes 1-1440.
- **Failed commands raise when no response is requested.** An automation that
  calls a `set_work_mode_*` action without `response_variable` now fails at
  that step if the command did not succeed, instead of carrying on silently.
  With `response_variable` the structured result is still always returned.
- **No more guessing the inverter.** An untargeted call on a site with more
  than one inverter, or a target that matches no inverter or several, is
  refused. Single-inverter sites are unaffected.
- **Actions have an "Inverter" field instead of a target.** `services.yaml`
  no longer declares `target` (hassfest now rejects the device filter it
  used, and a declared target is shown as required). `device_id` and
  `entity_id` are still accepted in calls, so existing automations keep
  resolving to the same inverter.
- **The API token moved out of Options.** It is changed through
  **Reconfigure** (or the reauthentication prompt). Options keep the update
  interval and the device-label override.
- **Single entry enforced by Home Assistant** (`single_config_entry`): a second
  setup aborts with the standard `single_instance_allowed` reason.
- **The device label comes from the API's `model` block.** The device `name`
  field is a provisioning default shared by every machine (the cause of
  "North Sea 6 (6kW)" showing for other models), so it is now only a
  fallback. Device cards relabel on upgrade; entity IDs do not change, and a
  user override still wins.
- **Positional MPPT / battery entities are migrated.** Entities that 1.x
  auto-discovered by list position (`pv_mppts_0_power`, ...) are moved in the
  entity registry to the new index-keyed entities (`mppt_1_power`, ...),
  keeping their entity IDs and history. Auto-discovered entities for hardware
  the model says is absent (a third tracker on a two-tracker machine, phase
  registers on a single-phase machine, the `model` block's own flags) are
  removed. Nothing is removed when the hardware cannot be determined.
- **License changed to MIT**, following upstream's relicensing.

### Added (ported from upstream)

- **Sign in with an Eleven Energy account** (OAuth2 + PKCE, Eleven Energy's
  public client, a fresh verifier per authorization (per flow since D35), `my.home-assistant.io`
  redirect). API tokens remain supported. Access tokens are refreshed by
  Home Assistant; a refresh never reloads the integration.
- **Reconfigure** to switch between signing in and an API token without
  losing devices, entities or history; **reauthentication** for both kinds
  of credential when the API rejects the stored one.
- **Machine-specific entities**: per-phase grid and system power / voltage /
  current on three-phase models; per-MPPT power / voltage / current (three
  trackers on dual-battery models, two otherwise); per-battery raw state of
  charge, power and module count (a battery fitted later is picked up
  live); rated AC / DC power. Phase entities share their group's category.
- **Device actions** for the seven work modes, delegating to the actions,
  validated when the automation is loaded.
- **Business names** in the UI: Self Consumption, Grid Charge, Grid Export,
  Excess PV Export, Idle Battery, Target State Of Charge, Reset Work Mode;
  action names and descriptions moved to translations, with upstream's
  corrected descriptions (for example: Grid Export's power is battery power,
  not power at the meter).
- `percentage_to_battery` accepted as an alias of `percent_to_battery`.
- Documentation that any work mode switches the Eleven Energy optimiser off
  until **Reset Work Mode** is sent.

### Added

- **Availability**: an inverter's entities go unavailable after 3
  consecutive failed polls, or immediately when the credential is rejected,
  and recover on the next good poll - automations no longer act on stale
  values. The Update Interval entity stays available.
- **Rejected credentials** (HTTP 401/403, refused token refresh) raise
  `ConfigEntryAuthFailed` at setup and start reauthentication at runtime;
  polling pauses rather than hammering a rejected credential (an API token
  is re-tested every 10 minutes, see D01 above).
- **Command ordering**: commands to one inverter run one at a time and the
  newest wins - an older command still waiting to retry is abandoned rather
  than overwriting a newer one later.
- **Retry policy**: only network errors, timeouts, 408, 429 (honouring
  `Retry-After`, capped at 30 s) and 5xx are retried; other 4xx fail at once.
- Manifest `categories: ["energy.solar_batteries"]` for the Marketplace.
- Diagnostics report the credential kind, auth state and per-inverter model,
  phases, trackers, batteries and availability; sign-in tokens are redacted.
- CI test workflow against Home Assistant 2026.3 (minimum) and latest; the
  test suite fails on any Home Assistant "Detected ..." report about this
  integration.

### Fixed

- **Actions disappeared after any reload** (token change, label change,
  "Reload") until Home Assistant restarted. They are registered once and no
  longer removed on unload.
- **`"nan"` became full power**: `"nan"` kW was clamped to 50 kW and `"nan"`
  % to 100 %. **`"inf"` minutes** raised an `OverflowError` out of a path
  documented never to raise. Booleans were accepted as numbers.
- **Binary sensors read the string `"false"` as on**, and started as off
  ("offline") before the first reading; they now parse strictly and start
  unknown.
- **Diagnostics did blocking file I/O in the event loop** (reading
  `manifest.json`), which Home Assistant reports; the version now comes from
  the integration loader.
- **Changing the token reloaded twice** (two update-listener calls racing);
  credential changes now reload exactly once.
- The options form reports invalid input with translated field errors.
- A non-object site payload crashed setup instead of retrying; malformed site
  and device payloads are now validated.
- A numeric sensor receiving text or NaN raised inside Home Assistant on
  every poll; it now reports unknown.
- Auto-discovery is capped at 200 entities per inverter.
- The token is entered in a password field and never shown pre-filled.
- Serial numbers are no longer logged at INFO on discovery.
- `hassfest` failures: `services.yaml` used a device filter on `target` and a
  `response` key, both now rejected.

### Changed

- Compatible with Home Assistant 2026.11: `DeviceInfo` built by keyword only,
  the percent unit without the deprecated `PERCENTAGE` name,
  `async_entity_id_changed` opt-in on every entity, `entry.runtime_data`,
  `ConfigFlowResult`, `AddConfigEntryEntitiesCallback`.
- README: Marketplace (2026.11+) and HACS (2026.3-2026.10) installation, the
  false "HACS Default" badge removed, release process documented.
- `actions/checkout@v7` in workflows; the development pin of
  `pytest-homeassistant-custom-component` raised to 0.13.375 (Home Assistant
  2026.10.4), with 0.13.320 (2026.3.4) kept in CI for the minimum version.

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

### Added

- **Device label override.** New optional text field in the OptionsFlow
  (Settings -> Devices & Services -> Eleven Energy Plus -> Configure ->
  "Device label override"). When set, it pins the device-card title and
  model to whatever the user types, regardless of what the
  `https://portal.elevenenergy.co.uk/api/v1/site` endpoint returns. Useful when the
  upstream API has the wrong product label for an inverter (e.g. a stale
  "North Sea 6" provisioning default against actual Mediterranean Sea 12
  hardware). Leaving the field blank preserves the historical
  API-derived behaviour. Changing the override triggers a brief
  integration reload so the new label is cleanly applied to the device
  registry; entity history is preserved.
- **First-discovery diagnostic log.** On the first sight of each
  inverter the integration now logs the full raw site-payload dict at
  INFO level
  (`Eleven Energy Plus discovered inverter <id>; raw site payload: ...`),
  so users investigating a mislabelled device can see exactly which
  fields the API populated without having to enable debug logging
  first.
- **Alternative label-field fallback.** When the site API's `name`
  field is empty or missing, the discovery code now tries
  `model` -> `productName` -> `inverterModel` in order before falling
  back to the generic "Inverter" placeholder. Installations with a
  populated `name` see no behaviour change.
- **HA Diagnostics platform.** A new `diagnostics.py` module
  implements Home Assistant's standard *Download diagnostics* button
  on the integration card. The dump captures the integration version,
  the config-entry data + options, the live controller state
  (poll interval, device-label override, count of known devices,
  registered platforms) and the most recent raw `/api/v1/site` and
  `/api/v1/devices/<id>` payloads. The API bearer token, device ids
  and serial numbers are redacted before the file is written, so the
  download is safe to attach to a public bug report. This replaces
  the first-discovery log line as the recommended way for users to
  share troubleshooting data when investigating a mislabelled
  inverter or unmapped API field.

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

### Branding

- **Local integration brand images at
  `custom_components/eleven_energy_plus/brand/`.** Added `icon.png`
  (256x256), `icon@2x.png` (512x512), `logo.png` (925x256),
  `logo@2x.png` (1850x512), `dark_logo.png` and `dark_logo@2x.png` so
  the Home Assistant UI shows the proper Eleven Energy artwork on the
  integration card under the new `eleven_energy_plus` domain instead
  of a placeholder. The artwork is reused verbatim from the original
  `eleven_energy` entry in
  [`home-assistant/brands`](https://github.com/home-assistant/brands)
  since both integrations target the same hardware vendor. The PR
  route into `home-assistant/brands` is no longer available - that
  repository's
  `close-new-custom-integrations.yml` workflow auto-closes any new
  `custom_integrations/` PR since Home Assistant 2026.3 introduced
  local brand image support for custom integrations (see
  <https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api>),
  so shipping the images directly inside the integration directory is
  now the supported path.
- **Minimum Home Assistant version: 2026.3.0.** `hacs.json` now
  declares `"homeassistant": "2026.3.0"` so HACS refuses to install
  or update Eleven Energy Plus on older Home Assistant releases. The
  constraint follows from the local `brand/` directory above: on
  HA < 2026.3 the Brands Proxy API hasn't been wired up yet and the
  brand images silently no-op (placeholder icon, no functional
  error), but gating at install time spares users from a partly-
  broken integration. Users who copy the integration into
  `custom_components/` manually (bypassing HACS) are not gated and
  will just see the placeholder icon on older HA. Eleven Energy
  Plus's Python code itself does not require any 2026.3-only API.

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
