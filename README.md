# Home Assistant Eleven Energy Plus integration

<a href="https://www.elevenenergy.co.uk"><img src="https://brands.home-assistant.io/eleven_energy/dark_logo.png" width="300px" alt="Eleven Energy website"/></a>

Connects to your Eleven Energy site (cloud connection) and obtains live stats from your inverter every 15 seconds by default (configurable from 5 to 300 seconds). It also allows changing of the hybrid work mode through service calls.

**Eleven Energy Plus** is the community-maintained fork of the original [`iPeel/HA-Eleven-Energy`](https://github.com/iPeel/HA-Eleven-Energy) integration. The HA integration domain is `eleven_energy_plus` (the original is `eleven_energy`), so the two HACS repositories install into different `custom_components/` folders and can coexist on the same Home Assistant instance.

| | Original | This fork |
| --- | --- | --- |
| HACS name | "Eleven Energy" | **"Eleven Energy Plus"** |
| Repository | `iPeel/HA-Eleven-Energy` | `goglecm/HA-Eleven-Energy` |
| Integration domain | `eleven_energy` | **`eleven_energy_plus`** |
| Filesystem path | `custom_components/eleven_energy/` | **`custom_components/eleven_energy_plus/`** |
| Service prefix | `eleven_energy.set_work_mode_*` | **`eleven_energy_plus.set_work_mode_*`** |

> If you previously installed the original integration, switching to the fork is a *replace*, not an in-place upgrade. Entities, automations and dashboards from the original will not carry over - you will need to remove the original integration, install Eleven Energy Plus, re-add the API token, and rewrite any automations using the new `eleven_energy_plus.*` service names.

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/goglecm/HA-Eleven-Energy)
![Project Maintenance][maintenance-shield]
[![GitHub Activity][commits-shield]][commits]

![image](https://github.com/user-attachments/assets/03d5442b-45a8-466d-b946-a090bafffff7)


## Installation

To use, add this repository through HACS and install the Eleven Energy Plus integration. You require HACS to be installed, follow the guide at [the HACS download site](https://www.hacs.xyz/docs/use/download/download/) for more details.

Once HACS is installed on your system:

* [Add the Eleven Energy Plus repository](https://my.home-assistant.io/redirect/hacs_repository/?owner=goglecm&repository=HA-Eleven-Energy&category=integration) to your HACS installation
* Click `Download`

You will also need an API token obtained through the Site & System Settings page of the Eleven Energy app. Once you have a token, from the Devices page in Home Assistant, add an integration, choose **"Eleven Energy Plus"** and add your API token when requested.

## Update Interval

The integration polls your inverter every 15 seconds by default. You can change this in two ways:

- **`number.{device_id}_poll_interval`** - a Number entity exposed on the inverter device. Set it from 5 to 300 seconds; the new cadence takes effect immediately.
- **Settings -> Devices & Services -> Eleven Energy Plus -> Configure** - lets you change the API token, the update interval, and the device label override.

Changing the interval from either surface wakes the background poller immediately, so the next API call lands at the new cadence rather than having to wait for the current sleep to expire.

## Device label override

If the Eleven Energy site API mislabels your hardware (e.g. it reports a stale "North Sea 6" against an actual Mediterranean Sea 12 inverter), open **Settings -> Devices & Services -> Eleven Energy Plus -> Configure** and fill in **Device label override** with the correct hardware string. The integration will then use it for both the device card title and the model field, regardless of what the API returns.

Leave the field blank to fall back to the API-derived label (the historical behaviour). Changing the override triggers a brief integration reload so the new label is applied cleanly to the device registry; entity history is preserved.

To verify what the API actually returns for your inverter, set Home Assistant to debug-log the integration (see [Troubleshooting](#troubleshooting)) or look at the INFO line logged on first discovery:

```
custom_components.eleven_energy_plus.controller: Eleven Energy Plus discovered inverter <id>; raw site payload: {...}
```

## Entities

### Curated entities

These are created up front for every discovered hybrid inverter, with units and device classes set so they integrate cleanly with the Home Assistant Energy dashboard, history graphs and templates. Names are localised through the Home Assistant translation system; the English defaults are shown below.

| Default name | Entity ID suffix | Unit | Type | Purpose |
| --- | --- | --- | --- | --- |
| PV Power | `_pv_power` | kW | Sensor (Power) | Current solar generation |
| PV Energy Today | `_pv_energy_today` | kWh | Sensor (Energy, TotalIncreasing) | Solar generated today |
| Consumption | `_load_power` | kW | Sensor (Power) | Current household load |
| Consumed Today | `_load_energy_today` | kWh | Sensor (Energy, TotalIncreasing) | Household energy consumed today |
| State Of Charge | `_state_of_charge` | % | Sensor (Battery) | Current battery SoC |
| Battery Power | `_battery_power` | kW | Sensor (Power) | Charge (-) / discharge (+) power |
| Charged Today | `_battery_energy_in_today` | kWh | Sensor (Energy, TotalIncreasing) | Energy into the battery today |
| Discharged Today | `_battery_energy_out_today` | kWh | Sensor (Energy, TotalIncreasing) | Energy out of the battery today |
| Grid Power | `_grid_power` | kW | Sensor (Power) | Import (+) / export (-) power |
| Imported Today | `_grid_energy_in_today` | kWh | Sensor (Energy, TotalIncreasing) | Grid import today |
| Exported Today | `_grid_energy_out_today` | kWh | Sensor (Energy, TotalIncreasing) | Grid export today |
| System Power | `_system_power` | kW | Sensor (Power, Diagnostic) | Whole-site power |
| System Voltage | `_system_voltage` | V | Sensor (Voltage, Diagnostic) | Site AC voltage |
| Status | `_system_status` | - | Sensor (Diagnostic) | Inverter status (`On Grid` / `Off Grid` / `Standby` / `Powered Off` / `Unknown`) |
| Work Mode | `_system_work_mode` | - | Sensor | Current work mode (translated) |
| Firmware Version | `_firmware_version` | - | Sensor (Diagnostic) | Inverter firmware string |
| Online | `_system_online` | - | Binary sensor (Connectivity, Diagnostic) | Cloud reachability |
| Update Interval | `_poll_interval` | s | Number (Config) | Configurable poll cadence |

> The old `sensor.{device_id}_system_online` entity from `iPeel/HA-Eleven-Energy` 1.2 and earlier is exposed at its correct `binary_sensor.{device_id}_system_online` location under this fork. Eleven Energy Plus uses a different integration domain (`eleven_energy_plus`) than the original, so no automatic registry migration is performed - users moving from the original integration get a fresh entity registry.

### Auto-discovered entities

In addition to the curated set, the integration walks the full API response on every poll and creates a sensor or binary sensor for every additional numeric, boolean or text field returned by the API. Units, device classes and icons are inferred from the field name; the current heuristics include:

| Field name pattern | Inferred entity |
| --- | --- |
| `*Power` | kW power sensor |
| `*ReactivePower`, `*Reactive` | var reactive-power sensor |
| `*ApparentPower`, `*Apparent` | VA apparent-power sensor |
| `*Energy*Today` | kWh `TotalIncreasing` energy sensor (today's bucket) |
| `*Energy*Total`, `*Energy*Lifetime` | kWh `TotalIncreasing` energy sensor (lifetime) |
| `rate`, `chargeRate`, `maxChargeRate`, etc. | kW battery charge/discharge rate sensor |
| `*Voltage` | V voltage sensor |
| `*Current` | A current sensor |
| `*Frequency` | Hz frequency sensor |
| `*Temperature`, `*Temp` | C temperature sensor |
| `stateOfHealth`, `soh` | % sensor |
| `stateOfCharge`, `soc`, `*Pc`, `*percent*` | % sensor (battery device class when on a battery path) |
| `*version*`, `model`, `firmware*`, `serial*` (string) | text sensor with chip icon |
| `*online*`, `*connected*` (bool) | binary connectivity sensor |
| `*fault*`, `*error*`, `*alarm*` (bool) | binary problem sensor |
| `*running*`, `*active*` (bool) | binary running sensor |
| anything else numeric | generic measurement sensor |
| anything else string | informational text sensor |

List-valued fields (e.g. per-string PV data) are expanded with index-based paths such as `pvStrings.0.power`, `pvStrings.1.power`, capped at 32 entries per list to keep pathological payloads from creating thousands of entities. Auto-discovered entities are tagged `Diagnostic` and can be disabled from the device page if you don't need them.

## Work Modes

The current Work Mode operating on each inverter is shown in the `sensor.{device_id}_system_work_mode` entity and is read only. To change work modes you can perform an Action ( Service Call in old money ) which allows you to specify additional attributes that control the work mode.

An action can be selected within an automation by selecting "Add action" then "Other actions" then "Perform an action", then select the appropriate service action from the list below. You then pick the inverter device to perform the action on, or leave the target blank and the first Hybrid Inverter on the site will be used.

Each action may require parameters as specified below, to use a parameter, add it as a JSON object in the Action data, for example:

![image](https://github.com/user-attachments/assets/7509b544-0a29-4979-93be-702f736bdc90)


### Service response

Every `set_work_mode_*` service supports an optional response (`supports_response: optional`) so automations can branch on the outcome. Capture it with `response_variable:`:

```yaml
- action: eleven_energy_plus.set_work_mode_force_charge
  target:
    device_id: !input inverter_device
  data:
    target_power: 3.5
    target_percent: 80
  response_variable: result
- if: "{{ not result.success }}"
  then:
    - action: notify.persistent_notification
      data:
        message: "Eleven Energy force-charge failed: {{ result.error }} (status {{ result.status }})"
```

The response is a dict with the following stable schema:

| Key | Type | Description |
| --- | --- | ----------- |
| `success` | `bool` | `true` only when the API returned HTTP 200. |
| `status` | `int \| null` | Last HTTP status from the API (`null` if no request was attempted). |
| `device_id` | `str \| null` | The Eleven Energy device id we actually targeted (may differ from `target.device_id` when the fallback "first inverter" path was taken). |
| `work_mode` | `str \| null` | The canonical API work mode posted, e.g. `selfConsumption`, `forceCharge`, `gridExport`, `pvExportPriority`, `idleBattery`, `targetSoc`, `reset`. |
| `params` | `dict` | The sanitised, clamped parameters we actually sent, keyed by the service-call field names (not the API's camelCase). |
| `attempts` | `int` | Number of HTTP attempts made (1-5). |
| `error` | `str \| null` | Human-readable reason on failure; `null` when `success` is `true`. |

Available work modes are as follows:

### Self Consumption

This is the default work mode, with excess solar diverted to the battery by default, and the battery supplying local loads if needed.

To switch to this work mode, use the "_set_work_mode_self_consumption_" service call with the following parameters:

Key | Required | Description
--- | --- | -----------
percent_to_battery | No | Sets the percentage of any excess solar that is diverted to the battery. For example as value of 50 will send half of the excess solar out to the grid and half into the battery.

### Force Charge

Charges the battery at a set power rate with energy from the grid. Once the target state of charge has been reached, the system will remain is a state where grid power is consumed to supply local loads until the work mode is changed.

To switch to this work mode, use the "_set_work_mode_force_charge_" service call with the following parameters:

Key | Required | Description
--- | --- | -----------
target_power | Yes | The amount of power in kW to charge the battery at.
target_percent | Yes | The target State Of Charge to charge the battery up to, a value of 100 will fully charge the battery.

### Grid Export

Force discharges the battery and uses excess solar to export to the grid at the specified rate until the target State of Charge is reached, thereafter the system will export all excess solar until the work mode is changed.

To switch to this work mode, use the "_set_work_mode_grid_export_" service call with the following parameters:

Key | Required | Description
--- | --- | -----------
target_power | Yes | The amount of power in kW to export to the grid, including excess solar and battery power.
target_percent | Yes | The target State Of Charge to discharge to.
include_excess_solar | No | When true, adds the average amount of excess PV over the consumed amount to the export, useful when ensuring the battery is discharged to a specified level.
overdrive | No | When true, allows the system to exceed the configured export limit while discharging.

### PV Export Priority

Exports all excess solar to the grid instead of charging the battery, if the amount of excess solar exceeds the export limit of the site then any excess is used to charge the battery.

To switch to this work mode, use the "_set_work_mode_pv_export_" service call with no parameters.

### Target State Of Charge

Manages grid export, battery charging and battery discharging based on a specified target state of charge and duration. The energy management system will then attempt to balance the amount of excess solar and battery to reach the specified state of charge by the end of the period, using an average of solar excess and available battery. A higher or lower state of charge goal may be specified than the currect state of charge, however the system will not force charge from the grid to reach a higher state of charge goal, only using excess solar to charge the battery.

To avoid target charging and export power reaching infinity or excessive values, the algorithm tops out at 30 minutes remaining. I.e. if the work mode stays in Target SoC mode or reaches within 30 minutes of the target period, the algorithm treats all calculations as if there are 30 minutes remaining to reach the target.

To switch to this work mode, use the "_set_work_mode_target_soc_" service call with the following parameters:

Key | Required | Description
--- | --- | -----------
target_soc | Yes | The desired state of charge in percent to reach at the end of the period.
target_minutes | Yes | The number of minutes from initiating this command to reach the target state of charge by.
max_charge_power | No | The amount of kilowatts max to charge the battery at. Defaults to the maximum charge rate of the battery.
max_discharge_power | No | The amount of kilowatts max to discharge the battery at. Defaults to the maximum discharge rate of the battery.

### Idle Battery

Allows the system to "coast" without using any battery, useful if you would prefer to reserve battery capacity for an upcoming period of high cost import.

To switch to this work mode, use the "_set_work_mode_idle_battery_" service call with the following parameters:

Key | Required | Description
--- | --- | -----------
allow_charging | No | Set to true if you still want to allow charging of the battery with excess solar i.e. only prohibit discharging.
allow_discharging | No | Set to true if you still want to allow discharging of the battery i.e. only prohibit charging.

### Reset

Resets the work mode based on the system configuration or any active schedules in the Eleven Energy app, returning control to the cloud-side scheduler.

To trigger a reset, use the "_set_work_mode_reset_" service call with no parameters.


## Troubleshooting

### Download diagnostics (recommended)

Eleven Energy Plus implements Home Assistant's standard **Diagnostics** platform. From **Settings -> Devices & Services -> Eleven Energy Plus**, click the three-dot menu next to the integration and choose **Download diagnostics**. You get a JSON file containing:

* the integration's version + domain,
* the entry's data and options (with the API token redacted),
* the live controller state (poll interval, device-label override, count of known devices, registered platforms), and
* the most recent raw `/api/v1/site` payload and the most recent raw `/api/v1/devices/<id>` payload per device.

Serial numbers and device ids are redacted before the file is written, so it is safe to attach to a public bug report. This is the recommended way to share troubleshooting data - it doesn't require enabling debug logging and captures everything the integration sees from the upstream API.

### Turning on debug logging

Add the following to your `configuration.yaml` (or use **Settings -> Devices & Services -> Eleven Energy -> Enable debug logging**) and restart Home Assistant:

```yaml
logger:
  default: warning
  logs:
    custom_components.eleven_energy_plus: debug
```

The first device payload received per Home Assistant process is logged once at `DEBUG`, truncated to 2000 characters. This is the most useful artefact to attach to a bug report when an entity isn't appearing or has the wrong unit.

### Common situations

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Setup keeps retrying with "Eleven Energy site API was unreachable during setup" | Bad token, expired token, or a transient API outage | Verify the token in the Eleven Energy app, then **Configure** the integration to update it |
| Setup retries with "no hybrid inverter devices" | The site has registered devices of other types only (e.g. EV charger), or the inverter hasn't reported in yet | Wait a few minutes; the integration retries automatically |
| Services succeed (`success: true`) but the inverter does not change mode | The API accepted the request but rejected it downstream | Inspect the response dict's `params` / `status`, and check the Eleven Energy app for an error |
| Service call returns `success: false, error: "no Eleven Energy device available"` | No hybrid inverter has been discovered yet | Wait for the initial poll to complete, or restart the integration |
| A dynamic entity has the wrong unit | The field name isn't matching any of the heuristics in the table above | Disable the entity from the device page and open an issue with the field's API path |

### Reporting an issue

Useful information to include:

1. A diagnostics download (see [Download diagnostics](#download-diagnostics-recommended) above) - this is by far the most useful single artefact and supersedes points 3-4 below in most cases.
2. Integration version (from **Settings -> Devices & Services -> Eleven Energy Plus**).
3. Home Assistant version.
4. A debug-level log snippet covering at least one full poll cycle (only if the diagnostics download isn't enough).
5. The INFO line logged on first discovery (search for `discovered inverter`).


## Development / Testing

The integration ships with a pytest-based test suite that exercises the config flow, controller HTTP / retry / termination behaviour, payload-walking + dynamic discovery, leaf-failure isolation, and the structured service response. To run it:

```bash
python3 -m venv .venv
.venv/bin/pip install -r dev-requirements.txt
.venv/bin/pytest tests/
```

To see a coverage report:

```bash
.venv/bin/pytest tests/ --cov=custom_components/eleven_energy_plus --cov-report=term-missing
```

The test suite uses [`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component) and runs entirely against an in-process Home Assistant instance with mocked aiohttp - no real network access is needed.


## Branding / icon

The Home Assistant integration card icon and logo for Eleven Energy Plus are shipped directly inside this repository at `custom_components/eleven_energy_plus/brand/` (`icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png`, `dark_logo.png`, `dark_logo@2x.png`). Since Home Assistant 2026.3, custom integrations serve their own brand images through HA's local brands proxy API rather than the [`home-assistant/brands`](https://github.com/home-assistant/brands) CDN, so no upstream PR is required - and in fact the brands repository's CI now auto-closes any new `custom_integrations/` submissions (see the [Brands Proxy API announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)). The artwork is reused verbatim from the original `eleven_energy` entry in the brands repository, since both integrations target the same Eleven Energy hardware.


## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a per-version summary of features, behaviour changes and bug fixes.


[commits-shield]: https://img.shields.io/github/commit-activity/y/goglecm/HA-Eleven-Energy.svg?style=for-the-badge
[commits]: https://github.com/goglecm/HA-Eleven-Energy/commits/master
[devcontainer]: https://code.visualstudio.com/docs/remote/containers
[license-shield]: https://img.shields.io/github/license/goglecm/HA-Eleven-Energy.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/maintenance/yes/2026.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/goglecm/HA-Eleven-Energy.svg?style=for-the-badge
[releases]: https://github.com/goglecm/HA-Eleven-Energy/releases
