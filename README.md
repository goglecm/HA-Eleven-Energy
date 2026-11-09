# Home Assistant Eleven Energy Plus integration

<a href="https://www.elevenenergy.co.uk"><img src="https://brands.home-assistant.io/eleven_energy/dark_logo.png" width="300px" alt="Eleven Energy website"/></a>

Connects Home Assistant to your Eleven Energy site through the Eleven Energy cloud, reads live data from your hybrid inverter every 15 seconds by default (configurable from 5 to 300 seconds), and lets automations change the inverter's work mode.

**Eleven Energy Plus** is an independent fork of [`iPeel/HA-Eleven-Energy`](https://github.com/iPeel/HA-Eleven-Energy). It uses its own integration domain, `eleven_energy_plus`, so it installs into a different `custom_components/` folder and can sit next to the original on the same Home Assistant instance.

| | Original | This fork |
| --- | --- | --- |
| Marketplace / HACS name | "Eleven Energy" | **"Eleven Energy Plus"** |
| Repository | `iPeel/HA-Eleven-Energy` | `goglecm/HA-Eleven-Energy` |
| Integration domain | `eleven_energy` | **`eleven_energy_plus`** |
| Filesystem path | `custom_components/eleven_energy/` | **`custom_components/eleven_energy_plus/`** |
| Action prefix | `eleven_energy.set_work_mode_*` | **`eleven_energy_plus.set_work_mode_*`** |

> Moving from the original integration to this fork is a *replace*, not an in-place upgrade: entities, automations and dashboards do not carry over. See [Moving from the original integration](#moving-from-the-original-integration).

![Project Maintenance][maintenance-shield]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

![image](https://github.com/user-attachments/assets/03d5442b-45a8-466d-b946-a090bafffff7)

Requires **Home Assistant 2026.3 or newer**. Tested against 2026.3, 2026.10 and 2026.11.

## Installation

Eleven Energy Plus is not in the community catalogue, so it is added as a custom repository.

### Home Assistant 2026.11 and newer (Marketplace)

Home Assistant 2026.11 brought HACS into Home Assistant itself as **Settings > Marketplace**; there is nothing to install first.

1. Open **Settings > Marketplace**. The first time, read the notice and select **I understand the risks and agree**.
2. Select **Add from link** (upper right) and enter `goglecm/HA-Eleven-Energy`. The Marketplace asks you to connect GitHub the first time you add a custom repository; the connection only reads public information.
3. Open **Eleven Energy Plus** and select **Install**. Setup opens by itself once the installation completes. If you close it, add the integration later from **Settings > Devices & services > Add integration**.

Updates appear under **Settings > Updates**. An installation previously downloaded through HACS carries over to the Marketplace and keeps updating.

### Home Assistant 2026.3 to 2026.10 (HACS)

1. [Add the Eleven Energy Plus repository](https://my.home-assistant.io/redirect/hacs_repository/?owner=goglecm&repository=HA-Eleven-Energy&category=integration) to HACS as a custom repository (category: Integration).
2. Select **Download**, then restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration** and choose **Eleven Energy Plus**.

### Manually

Copy `custom_components/eleven_energy_plus/` from the [latest release](https://github.com/goglecm/HA-Eleven-Energy/releases) into your Home Assistant `config/custom_components/` folder, restart, and add the integration.

## Connecting to Eleven Energy

Setup offers two ways in:

* **Sign in with an Eleven Energy account** (recommended) - you are sent to Eleven Energy's own sign-in page to log in and approve the connection. Home Assistant never sees a password, the connection is scoped to your site, Home Assistant refreshes it automatically, and you can withdraw it at any time from the Eleven Energy app. The redirect back to Home Assistant goes through `my.home-assistant.io`.
* **Use an API token** - paste a token created in the Eleven Energy app under **Site & System Settings > Access > Add Access**. Right for headless setups. The token is checked with Eleven Energy before it is saved. Tokens lapse after 90 days without being used.

Only one Eleven Energy Plus site can be configured per Home Assistant instance.

**Switching later:** choose **Reconfigure** from the integration's menu (**Settings > Devices & services > Eleven Energy Plus**) to move between signing in and an API token. Devices, entities, entity IDs and history are kept; only the way Home Assistant authenticates changes.

**When the credential stops working** (a token revoked or lapsed, a sign-in withdrawn), Eleven Energy Plus marks every entity *unavailable*, stops polling, and Home Assistant shows a reauthentication prompt under **Settings > Devices & services** asking for a new token (for a token entry) or a fresh sign-in (for a signed-in entry). Nothing is retried against the rejected credential in the meantime.

## Options

**Settings > Devices & services > Eleven Energy Plus > Configure** has two settings:

* **Update interval** - how often inverter data is fetched, 5 to 300 seconds. It is also exposed as the `number.{device_id}_poll_interval` entity on the inverter device. Either way the new cadence applies immediately.
* **Device label override** - see below.

The credential is changed through Reconfigure, not here.

## Device label

The device card is named after the product the Eleven Energy API reports in its `model` block, for example "Eleven Energy Plus Mediterranean Sea 12". The device's own `name` field is a provisioning default that is the same on every machine (which is why some installations used to show "North Sea 6 (6kW)" for a different model), so it is only used when the API sends no model block.

If the label is still wrong, fill in **Device label override** in the options. It wins over everything the API reports, for both the device card title and the model field. Leave it blank to use the API's label. Changing it reloads the integration; entity IDs and history are kept.

## Entities

### Every inverter

These are created for every hybrid inverter, with units and device classes that work with the Energy dashboard, statistics and templates. Names are translated; the English defaults are shown.

| Default name | Entity ID suffix | Unit | Type | Purpose |
| --- | --- | --- | --- | --- |
| PV Power | `_pv_power` | kW | Sensor (Power) | Current solar generation |
| PV Energy Today | `_pv_energy_today` | kWh | Sensor (Energy, TotalIncreasing) | Solar generated today |
| Consumption | `_load_power` | kW | Sensor (Power) | Current household load |
| Consumed Today | `_load_energy_today` | kWh | Sensor (Energy, TotalIncreasing) | Household energy consumed today |
| State Of Charge | `_state_of_charge` | % | Sensor (Battery) | Usable battery state of charge |
| Battery Power | `_battery_power` | kW | Sensor (Power) | Charge (-) / discharge (+) power |
| Charged Today | `_battery_energy_in_today` | kWh | Sensor (Energy, TotalIncreasing) | Energy into the battery today |
| Discharged Today | `_battery_energy_out_today` | kWh | Sensor (Energy, TotalIncreasing) | Energy out of the battery today |
| Grid Power | `_grid_power` | kW | Sensor (Power) | Import (+) / export (-) power |
| Imported Today | `_grid_energy_in_today` | kWh | Sensor (Energy, TotalIncreasing) | Grid import today |
| Exported Today | `_grid_energy_out_today` | kWh | Sensor (Energy, TotalIncreasing) | Grid export today |
| System Power | `_system_power` | kW | Sensor (Power, Diagnostic) | Whole-system power |
| System Voltage | `_system_voltage` | V | Sensor (Voltage, Diagnostic) | System AC voltage |
| Status | `_system_status` | - | Sensor (Diagnostic) | `On Grid` / `Off Grid` / `Standby` / `Powered Off` / `Unknown` |
| Work Mode | `_system_work_mode` | - | Sensor | Current work mode |
| Firmware Version | `_firmware_version` | - | Sensor (Diagnostic) | Inverter firmware |
| Online | `_system_online` | - | Binary sensor (Connectivity, Diagnostic) | Whether the inverter is reporting to the cloud |
| Update Interval | `_poll_interval` | s | Number (Config) | Poll cadence |

### Depending on the hardware

Which of these exist is decided from the API's model description and the first reading, not from which registers happen to be present - every machine reports three solar-tracker register sets, for example, whether or not it has three trackers.

| Default name | Entity ID suffix | When | Category |
| --- | --- | --- | --- |
| Grid Power L1 / L2 / L3 | `_grid_phases_L1_power` ... | Three-phase models | Main |
| Grid Voltage / Current L1-L3 | `_grid_phases_L1_voltage`, `_grid_phases_L1_current` ... | Three-phase models | Diagnostic |
| System Power / Voltage / Current L1-L3 | `_system_phases_L1_power` ... | Three-phase models | Diagnostic |
| MPPT *n* Power / Voltage / Current | `_mppt_1_power`, `_mppt_1_voltage`, `_mppt_1_current` ... | One set per solar tracker: three on dual-battery models, two otherwise | Diagnostic |
| Battery *n* Raw State Of Charge | `_battery_1_raw_state_of_charge` | One per fitted battery | Diagnostic |
| Battery *n* Power | `_battery_1_power` | One per fitted battery | Diagnostic |
| Battery *n* Modules | `_battery_1_modules` | One per fitted battery | Diagnostic |
| Rated AC Power / Rated DC Power | `_model_maxAcPower`, `_model_maxDcPower` | When the model reports its ratings | Diagnostic |

*Battery n Raw State Of Charge* is the battery management system's own figure, before the top and bottom buffer allowances, so it does not match the usable *State Of Charge*. Solar-tracker and battery readings are keyed by each item's own index, so a reading that ever omits one cannot shift another's values onto it. A battery fitted later gets its entities as soon as the API reports it.

### Auto-discovered entities

Anything else the API returns is exposed as a diagnostic sensor or binary sensor, with units, device classes and icons inferred from the field name:

| Field name pattern | Inferred entity |
| --- | --- |
| `*Power` | kW power sensor |
| `*ReactivePower`, `*Reactive` | var reactive-power sensor |
| `*ApparentPower`, `*Apparent` | VA apparent-power sensor |
| `*Energy*Today` | kWh `TotalIncreasing` energy sensor (today's bucket) |
| `*Energy*Total`, `*Energy*Lifetime` | kWh `TotalIncreasing` energy sensor (lifetime) |
| `rate`, `chargeRate`, `maxChargeRate`, etc. | kW charge/discharge rate sensor |
| `*Voltage` | V voltage sensor |
| `*Current` | A current sensor |
| `*Frequency` | Hz frequency sensor |
| `*Temperature`, `*Temp` | °C temperature sensor |
| `stateOfHealth`, `soh` | % sensor |
| `stateOfCharge`, `soc`, `*Pc`, `*percent*` | % sensor (battery device class on a battery path) |
| `*version*`, `model`, `firmware*`, `serial*` (string) | text sensor with chip icon |
| `*online*`, `*connected*` (bool) | binary connectivity sensor |
| `*fault*`, `*error*`, `*alarm*` (bool) | binary problem sensor |
| `*running*`, `*active*` (bool) | binary running sensor |
| anything else numeric | generic measurement sensor |
| anything else string | informational text sensor |

Lists are expanded with index-based paths (`someList.0.value`), capped at 32 entries per list, and at most 200 auto-discovered entities are created per inverter. Auto-discovered entities can be disabled from the device page if you don't need them.

### Availability and values you can trust

* If **3 polls in a row** fail for an inverter (about 45 seconds at the default interval), all of its entities become **unavailable** rather than showing stale values; they recover on the next successful poll. A rejected credential makes them unavailable immediately. The Update Interval entity stays usable throughout.
* A numeric sensor only ever reports a finite number. Anything else the API sends for it (text, NaN, a boolean) reads as *unknown*, with one warning in the log.
* Binary sensors only accept unambiguous values (`true`/`false`, `on`/`off`, `1`/`0`); anything else reads as *unknown*. They are *unknown* until the first reading arrives.

## Work modes

> **Setting any work mode switches the Eleven Energy optimiser off for that site, and it stays off until you send `Reset Work Mode`.** An automation that takes control should hand it back when it is done.

The current work mode is shown by the read-only `sensor.{device_id}_system_work_mode` entity. To change it, use one of the actions below: in an automation, select **Add action**, search for **Eleven Energy Plus** (or the mode's name, such as *Grid Charge*), and fill in its fields. In YAML, the action names are `eleven_energy_plus.set_work_mode_*` and the field keys are listed in each section.

**Choosing the inverter.** Each action has an optional **Inverter** field (`device_id`). On a site with one inverter you can leave it empty. On a site with more than one, a call without it is refused rather than sent to whichever inverter happens to be first. For automations written against earlier versions, `entity_id` (an entity of the inverter) and `target:` are still accepted.

**Validation.** Every field is checked before anything is sent to the inverter. A value out of range, `nan`/infinity, text where a number is expected, an ambiguous boolean, an unknown field, or a missing required field makes the action fail with a clear error. Values are never clamped or dropped silently. Numbers from templates (for example `"80"`) are accepted.

| Field kind | Accepted range |
| --- | --- |
| Percentages (`percent_to_battery`, `target_percent`, `target_soc`) | 0 - 100 |
| Powers (`target_power`, `max_charge_power`, `max_discharge_power`) | 0 - 50 kW (the UI slider stops at 20 kW) |
| `target_minutes` | Whole minutes, 1 - 1440 |

**When a command fails.** Commands are retried only when retrying can help (network errors, timeouts, rate limiting, server errors), up to 5 attempts over about 15 seconds. Commands to the same inverter run one at a time, and the newest one wins: an older command still waiting to retry is abandoned when a newer one is issued.

* If the automation does **not** ask for a response, a failed command **raises an error**, so the automation step fails visibly (and shows in its trace) instead of carrying on as if the mode had changed.
* If it **does** ask for a response (`response_variable:`), the result is always returned, success or not, so the automation can decide what to do:

```yaml
- action: eleven_energy_plus.set_work_mode_force_charge
  data:
    device_id: !input inverter_device   # optional on a single-inverter site
    target_power: 3.5
    target_percent: 80
  response_variable: result
- if: "{{ not result.success }}"
  then:
    - action: notify.persistent_notification
      data:
        message: "Grid Charge failed: {{ result.error }} (status {{ result.status }})"
```

| Response key | Type | Description |
| --- | --- | --- |
| `success` | `bool` | `true` only when the API accepted the command (HTTP 200). |
| `status` | `int \| null` | Last HTTP status from the API (`null` if nothing was sent). |
| `device_id` | `str \| null` | The Eleven Energy device id the command was for. |
| `work_mode` | `str \| null` | The API work mode sent: `selfConsumption`, `forceCharge`, `gridExport`, `pvExportPriority`, `idleBattery`, `targetSoc` or `reset`. |
| `params` | `dict` | The validated parameters sent, keyed by the action's field names. |
| `attempts` | `int` | HTTP attempts made (0 - 5). |
| `error` | `str \| null` | Why it failed; `null` on success. |

**Device actions.** The same seven modes are also offered as device actions, delegating to the actions above with the same fields and limits. Recent Home Assistant automation editors no longer list device actions, so the actions above are the way to go; device actions remain for YAML and older editors (`type: self_consumption`, `force_charge`, `grid_export`, `pv_export`, `idle_battery`, `target_soc`, `reset`).

### Self Consumption

`eleven_energy_plus.set_work_mode_self_consumption`

Solar covers the house, surplus charges the battery, and once it is full the surplus is exported. The battery makes up any shortfall.

| Label | Key | Required | Description |
| --- | --- | --- | --- |
| Percentage To Battery | `percent_to_battery` | No | How much of the surplus solar to store rather than export, as a percentage. Leave unset to store all of it. (`percentage_to_battery` is accepted as an alias.) |

### Grid Charge

`eleven_energy_plus.set_work_mode_force_charge`

Charges the battery from the grid at a set power until it reaches a target. Once there, the house is supplied from the grid until the mode is changed.

| Label | Key | Required | Description |
| --- | --- | --- | --- |
| Target Power | `target_power` | Yes | Battery charge power in kW. A lower rate may be used if the battery is current limited, such as at a low state of charge. |
| Target SoC % | `target_percent` | Yes | The state of charge to stop charging at. |

### Grid Export

`eleven_energy_plus.set_work_mode_grid_export`

Discharges the battery to the grid at a set power until it reaches a target. After that the house is still supplied from the battery, but surplus solar is exported rather than stored.

| Label | Key | Required | Description |
| --- | --- | --- | --- |
| Target Power | `target_power` | Yes | Battery discharge power in kW. This is battery power, not power at the meter: with 1 kW of house load, 5 kW here puts about 4 kW on the grid. |
| Target SoC % | `target_percent` | Yes | The state of charge to stop discharging at. |
| Add Surplus Solar | `include_excess_solar` | No | Export surplus solar on top of the discharge power rather than storing it. |
| Overdrive | `overdrive` | No | Allow exceeding the usual export ceiling where the connection permits it. Leave off unless you have been told otherwise. |

### Excess PV Export

`eleven_energy_plus.set_work_mode_pv_export`

Like Self Consumption, but surplus solar is exported before it is stored. If more solar is available than can be exported, the remainder goes to the battery. No parameters.

### Target State Of Charge

`eleven_energy_plus.set_work_mode_target_soc`

Reaches a target state of charge within a deadline, working out the rate from the house load as it goes, using excess solar and the battery (it does not force-charge from the grid). Once the target is reached the system runs in Excess PV Export until the mode is changed. To keep rates sensible, the algorithm treats anything under 30 minutes remaining as 30 minutes.

| Label | Key | Required | Description |
| --- | --- | --- | --- |
| Target SoC % | `target_soc` | Yes | The state of charge to aim for. |
| Target Minutes | `target_minutes` | Yes | How long it has to get there, in whole minutes (1 - 1440). |
| Max Charge Power | `max_charge_power` | No | A ceiling in kW while charging. Defaults to the battery's maximum. |
| Max Discharge Power | `max_discharge_power` | No | A ceiling in kW while discharging. Defaults to the battery's maximum. |

### Idle Battery

`eleven_energy_plus.set_work_mode_idle_battery`

Holds the battery where it is, for example to keep capacity for an upcoming expensive period. Charging and discharging can be allowed separately.

| Label | Key | Required | Description |
| --- | --- | --- | --- |
| Allow Charging | `allow_charging` | No | Permit charging the battery from surplus solar (i.e. only prohibit discharging). |
| Allow Discharging | `allow_discharging` | No | Permit discharging the battery to supply the house (i.e. only prohibit charging). |

### Reset Work Mode

`eleven_energy_plus.set_work_mode_reset`

Hands control back to the system. If a schedule was in effect when the mode was changed it resumes, otherwise automatic operation does. Send this when your automation has finished. No parameters.

## Moving from the original integration

The two integrations use different domains, so Home Assistant treats them as unrelated:

1. Install Eleven Energy Plus (above) and set it up - sign in, or create a new API token.
2. Update automations and scripts from `eleven_energy.set_work_mode_*` to `eleven_energy_plus.set_work_mode_*`, and dashboards to the new entities (same entity ID suffixes, see [Entities](#entities)).
3. Remove the original integration once you are happy.

Both can run side by side while you switch over, but don't let two integrations send work-mode commands to the same inverter.

## Troubleshooting

### Download diagnostics (recommended)

From **Settings > Devices & services > Eleven Energy Plus**, open the three-dot menu and choose **Download diagnostics**. The JSON file contains the integration version, the entry's options, how it connects (sign-in or API token), what the integration knows about each inverter (model, phases, trackers, batteries, availability) and the most recent raw API responses. The API token, sign-in tokens, serial numbers and device ids are redacted, so it is safe to attach to a public bug report.

### Turning on debug logging

Use **Settings > Devices & services > Eleven Energy Plus > Enable debug logging**, or add to `configuration.yaml` and restart:

```yaml
logger:
  default: warning
  logs:
    custom_components.eleven_energy_plus: debug
```

At debug level the raw site listing for each newly discovered inverter and the first device reading of each Home Assistant run are logged (the latter truncated to 2000 characters).

### Common situations

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| A **Reauthenticate** prompt appears and the entities are unavailable | The token was revoked or lapsed (90 days unused), or the sign-in was withdrawn | Follow the prompt: enter a new token or sign in again |
| Setup keeps retrying ("unreachable during setup") | The Eleven Energy API can't be reached right now | Nothing; Home Assistant retries automatically |
| Setup retries with "no hybrid inverter devices" | The site only lists other device types, or the inverter hasn't reported yet | Wait; the integration retries automatically |
| Entities show *unavailable* | Three or more polls in a row failed | Check the log for the reason; they recover by themselves when polls succeed |
| An action fails with "this site has more than one inverter" | No inverter was chosen on a multi-inverter site | Fill in the **Inverter** field |
| An action fails with a validation error | A value is out of range or the wrong type | Fix the value; see the limits under [Work modes](#work-modes) |
| The inverter is back to automatic operation sooner or later than expected | The optimiser stays off after any work mode until **Reset Work Mode** | Send Reset Work Mode when your automation is done |
| An auto-discovered entity has the wrong unit | Its field name matches the wrong heuristic | Disable it and open an issue with its API path (diagnostics shows it) |

### Reporting an issue

Please include a diagnostics download, the integration and Home Assistant versions, and - if the diagnostics aren't enough - a debug log covering at least one poll cycle.

## Development / Testing

The pytest suite covers the config, reconfigure, reauthentication and options flows (including account sign-in with mocked endpoints), the controller's HTTP / retry / ordering / availability behaviour, payload walking and the model-driven entities, the registry migration, the actions and device actions, diagnostics, and static checks on `manifest.json`, `services.yaml` and the translations. Any Home Assistant "Detected ..." deprecation or blocking-call report about this integration fails the test that caused it.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r dev-requirements.txt
.venv/bin/pytest
.venv/bin/pytest --cov=custom_components/eleven_energy_plus --cov-report=term-missing
```

CI (`.github/workflows/`) runs the suite against the minimum supported Home Assistant (2026.3) and the latest version the test package supports, plus hassfest and the HACS validation, on every push.

### Releasing

The Marketplace and HACS install and update from GitHub **releases**. To publish a version: bump `version` in `custom_components/eleven_energy_plus/manifest.json`, add a `CHANGELOG.md` entry, tag the commit `vX.Y.Z` (matching the manifest), push the tag, and create a GitHub release from it.

## Branding / icon

The integration's icon and logo ship inside the integration at `custom_components/eleven_energy_plus/brand/`. Since Home Assistant 2026.3 custom integrations serve their own brand images (see the [brands proxy API announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)); that is also why `hacs.json` requires Home Assistant 2026.3. The artwork is the Eleven Energy artwork also used by the original integration.

## License

MIT - see [LICENSE](LICENSE). The original integration was relicensed to MIT by its author in October 2026; this fork follows.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).


[commits-shield]: https://img.shields.io/github/commit-activity/y/goglecm/HA-Eleven-Energy.svg?style=for-the-badge
[commits]: https://github.com/goglecm/HA-Eleven-Energy/commits/main
[license-shield]: https://img.shields.io/github/license/goglecm/HA-Eleven-Energy.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/maintenance/yes/2026.svg?style=for-the-badge
