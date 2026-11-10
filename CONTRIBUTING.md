# Working on Eleven Energy Plus

Eleven Energy Plus controls a home battery inverter, so a change is finished
only when it is tested, typed, linted and documented, on every supported Home
Assistant version. This guide covers how to set up, how the tests are
organised, when and how to add them, the gates a change must pass, and how
to release. How the code itself works is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); read that first.

## Set up

Python 3.14 is required.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r dev-requirements.txt
```

`dev-requirements.txt` pins everything: `pytest-homeassistant-custom-component`
(which brings Home Assistant itself, pytest, pytest-asyncio, pytest-cov,
pytest-xdist, pytest-freezer and Home Assistant's test helpers), hypothesis,
ruff, mypy, vulture and mutmut. Each test-package release pins one Home
Assistant version; 0.13.375 is Home Assistant 2026.10.4. Dependabot proposes
updates weekly.

### The other supported Home Assistant versions

The integration supports Home Assistant 2026.3 and newer. Before a change is
merged, run the suite on the oldest version and on the newest one too. CI
does both on every push; to do it locally, use two more virtual
environments:

```bash
# Oldest supported: Home Assistant 2026.3.4
python3.14 -m venv /tmp/venv-floor
/tmp/venv-floor/bin/pip install pytest-homeassistant-custom-component==0.13.320 hypothesis==6.170.15 mypy==2.4.1
/tmp/venv-floor/bin/python -m pytest

# Newest Home Assistant, usually ahead of any test package
python3.14 -m venv /tmp/venv-newest
/tmp/venv-newest/bin/pip install pytest-homeassistant-custom-component hypothesis==6.170.15 mypy==2.4.1
/tmp/venv-newest/bin/pip install --upgrade --no-deps homeassistant
/tmp/venv-newest/bin/python tests/compat/install_missing_requirements.py
/tmp/venv-newest/bin/python tests/compat/run_newest_ha.py
```

`tests/compat/run_newest_ha.py` runs pytest after applying the shims a newer
Home Assistant needs on an older test package. It takes pytest's arguments
and never touches the integration. Run it without `-n`: the shims are applied
in its own process only, not in parallel workers.

## Repository layout

| Path | What it is |
| --- | --- |
| `custom_components/eleven_energy_plus/` | The integration (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)). |
| `tests/test_<module>.py` | One test file per integration module: `test_controller.py`, `test_hybrid_inverter.py`, `test_notifications.py`, `test_services.py`, `test_config_flow.py`, `test_oauth.py`, `test_number.py`, `test_sensor.py`, `test_binary_sensor.py`, `test_device_action.py`, `test_diagnostics.py`, `test_migration.py`, `test_parsing.py`, `test_const.py`, `test_init.py`. |
| `tests/test_helpers.py` | Pure-function tests for helpers in several modules. |
| `tests/test_defect_classes.py` | Defect sweeps: for each defect class, every place it could occur. |
| `tests/test_lifecycle_races.py` | Overlaps: deletion, unload, reload and relisting landing while a request is in flight. |
| `tests/test_static.py` | Metadata: `manifest.json`, `services.yaml`, translations against the code. |
| `tests/test_static_analysis.py` | ruff, mypy `--strict`, vulture, public imports, silent `except` blocks, unused translation keys, the README-to-model mapping, the ledger. |
| `tests/common.py` | Payload builders and entry helpers shared by the tests. |
| `tests/conftest.py` | Autouse fixtures, `instant_backoff`, and the functional-coverage and defect-report plugins. |
| `tests/functional_coverage.py` | The functional coverage model. |
| `tests/DEFECTS.md` | The defect ledger. |
| `tests/*_report.md` | Generated: functional coverage, red check, mutation testing. Commit them with the change that alters them. |
| `tests/tools/` | `check_red.py`, `check_mutants.py`, `write_mutation_report.py`, `classify_mutants.py`. |
| `tests/compat/` | The newest-Home-Assistant runner and its installer. |
| `ruff.toml`, `mypy.ini`, `vulture_whitelist.py`, `pytest.ini`, `pyproject.toml` | Tool configuration (coverage and mutmut live in `pyproject.toml`). |

## Running the tests

```bash
.venv/bin/pytest                                  # the full suite
.venv/bin/pytest -n auto                          # the same, in parallel
.venv/bin/pytest tests/test_controller.py         # one file
.venv/bin/pytest -k "retry_after"                 # by name
```

A run of the whole `tests/` directory is a **full run**: it enforces the
functional-coverage gate and rewrites `tests/functional_coverage_report.md`,
in parallel (`-n`) as well as serially. A partial run (a file, `-k`, `-m`,
`--lf`) leaves both alone, so the committed report always describes the
whole suite. Any Home Assistant
"Detected ..." deprecation or blocking-call report, "not strictly
increasing" or negative-counter warning that mentions this integration fails
the test that caused it, and a `DeprecationWarning` raised by the
integration's own code is an error.

### Coverage

The target is **100 % line and branch coverage** of the integration, and
the run fails below it (`fail_under = 100` in `pyproject.toml`). There are no
`pragma: no cover` comments; the only exclusion is `if TYPE_CHECKING:`
import blocks, which never run. CI runs:

```bash
.venv/bin/pytest -n auto \
  --cov=custom_components/eleven_energy_plus \
  --cov-branch --cov-report=term-missing --cov-fail-under=100
```

`--cov-report=term-missing` lists every line and branch left uncovered; add
`--cov-report=html` for a browsable report in `htmlcov/`. Coverage is a
floor, not the goal: a line that ran is not a behaviour that was checked,
which is what functional coverage is for.

### Functional coverage

`tests/functional_coverage.py` lists every behaviour the integration
promises, each with a stable id (`FC-AUTH-01`, `FC-CMD-03`, ...), and the
**cross cells** (`X-...`): combinations that must each be exercised, such as
every credential entry point with every outcome, every action with every
failure kind in both response modes, every entity kind under every cause of
unavailability, and every numeric field with every kind of invalid value.

A test declares what it verifies:

```python
@pytest.mark.covers("FC-CMD-02")
async def test_retry_after_as_an_http_date(...): ...

@pytest.mark.parametrize("cause", [pytest.param("auth", marks=pytest.mark.covers("X-AVAIL-sensor-auth"))])
```

Only passing tests count. A full run fails when an item or cell is
uncovered, or a marker names an id that does not exist.
`--functional-coverage=strict` enforces on any run (so a partial run fails
for what it leaves out), `report` writes the report on any run without
enforcing (do not commit one from a partial run), and `off` skips the plugin
(the red check and mutation runs use `off`). A marker naming an unknown id
fails any run except with `off`.

The README's behavioural sections are tied to the model: every paragraph and
list item there ends with an `<!-- FC-... -->` comment naming the items it
states, and `test_every_readme_behaviour_maps_to_the_model` fails on an
untagged statement or an unknown id.

## When to add tests

| Change | Tests to add |
| --- | --- |
| A new or changed behaviour | A test for it, tagged `covers(...)`; a new model item if no existing one states it; the README statement, tagged with that item. |
| A new input (field, option, payload key) | Cases for every kind of invalid value it can receive, and the cross cells for it. |
| A bug | The defect protocol below: tests that fail first, at the place found and everywhere else the same class of bug could be. |
| A new await point that something else could interleave with | A race test in `tests/test_lifecycle_races.py` for each lifecycle step that could land there (unload, reload, device deletion, credential rejection). |
| Anything version-dependent | The suite on all three Home Assistant versions; the public-import check covers new imports. |
| A change to a mutation-tested module | A fresh mutation run; kill new survivors or justify them. |

A test with no assertion about behaviour - one that only runs code - does
not count, and mutation testing will show it.

## How to write a test

Drive the integration through the surfaces Home Assistant uses: set up an
entry, mock the API, and assert on entity states, service results, issues
and logs. Reach for a private method only to put the code in a state that is
otherwise slow to reach (for example `controller._poll_cycle()` instead of
waiting for the poller).

```python
import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.eleven_energy_plus.const import BASE_URL

from .common import DEVICE_ID, api_key_entry, mock_api, setup_entry


@pytest.mark.covers("FC-AVAIL-01")
async def test_three_failed_polls_make_the_inverter_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    controller = entry.runtime_data

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
    for _ in range(3):
        await controller.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.{DEVICE_ID}_pv_power").state == "unavailable"
```

Helpers and fixtures:

* `tests/common.py`: `site_summary`, `site_payload`, `device_payload` (a
  realistic reading; override any part), `mock_api` (site plus one mock per
  device), `api_key_entry`, `setup_entry`, `restore_sensor_value` (a restored
  state on every Home Assistant version, including 2026.11's
  registry-keyed restore cache), `device_field` (read `DeviceInfo` on any
  version).
* From Home Assistant's test helpers: `hass`, `aioclient_mock` (unmatched
  requests raise, so register everything a test can reach), `hass_storage`,
  `freezer`, `caplog`, `current_request_with_host` (for OAuth flows),
  `MockConfigEntry`, `async_fire_time_changed`.
* From `tests/conftest.py`: `instant_backoff` makes command retry waits
  instant and yields the delays asked for.

Time:

* Code that measures durations uses `time.monotonic()` through its module's
  `time` import. Replace the module's `time` with a fake that has a
  `monotonic()` method (see `FakeClock` in `tests/test_notifications.py`);
  never patch the global `time.monotonic`, which the event loop uses.
* `freezer` moves wall-clock time; it does not fire Home Assistant timers.
  Use `async_fire_time_changed` for those.

Logs: assert on the records of the integration's own logger
(`record.name == "custom_components.eleven_energy_plus.controller"`), not
on any record or on `caplog.text` by a single word. On a loaded machine
asyncio logs slow-task warnings that contain the test's own name, which
would satisfy, or break, a loose assertion.

Races: hold a request open with an `asyncio.Event`, perform the overlapping
step, release it, and assert on what is left (see `_gated_post` in
`tests/test_lifecycle_races.py`).

Fuzzing: hypothesis tests (in `test_hybrid_inverter.py`, `test_parsing.py`,
`test_services.py`) cover the payload walker, parsers and schemas. Keep
hypothesis tests at module level: on class methods its health check
misfires under mutation testing.

## The defect protocol

Every bug, however small, goes through the same steps, recorded in
[`tests/DEFECTS.md`](tests/DEFECTS.md):

1. **Ledger entry.** Add a row: the next id, module, defect class, impact,
   and the baseline (below).
2. **Tests before the fix.** A direct test, plus a sweep of every other place
   the same class of defect could occur. Tag the cases that catch it
   `@pytest.mark.defect("Dnn")`, and the sweep cases where the code was
   already right `@pytest.mark.guard("Dnn")`.
3. **Red.** Run them on the unfixed code; every defect test must fail for
   the stated reason. Record what failed in the ledger's red evidence
   column.
4. **Fix**, then **green**: the whole suite passes on all three Home
   Assistant versions; record that in the green evidence column.
5. **Re-prove red** with the tool, and commit its report:

   ```bash
   .venv/bin/python tests/tools/check_red.py
   ```

`check_red.py` creates a git worktree of each baseline commit, copies the
current `tests/` and tool configuration into it, runs every defect and guard
test there, and requires every defect test of an entry to fail for a real
reason (an `ImportError` of the integration does not count) and every guard
to pass. The baseline column says which commit:

* `fails`: the defect exists in `29b385d`, the code before the audit;
* `fails at <sha>`: the defect was introduced later, by that commit;
* `n/a (reason)`: nothing to fail against (a missing test, test tooling);
  such entries are proved by guard tests.

Defect tests must only use names that exist on their baseline: a test
module that imports something the fix introduces will not import there, and
its tests count as import failures. Import the module and read the name
inside the test (`oauth.MAX_PENDING_SIGN_INS`), or put the test in a module
whose imports exist on the baseline. `test_ledger_and_markers_agree` checks
that every ledger row has tagged tests and every tag names a row.

## Static gates

```bash
.venv/bin/ruff check custom_components tests
.venv/bin/mypy custom_components/eleven_energy_plus          # also on /tmp/venv-floor and /tmp/venv-newest
.venv/bin/vulture custom_components tests vulture_whitelist.py --min-confidence 60
```

* **ruff** with a broad rule set (`ruff.toml`); a `noqa` comment that is no
  longer needed fails too (RUF100).
* **mypy `--strict`** (`mypy.ini`) on the oldest and newest Home Assistant:
  their typing differs.
* **vulture**: an entry in `vulture_whitelist.py` is for a name only Home
  Assistant or pytest uses (platform hooks, entity properties), with a
  comment saying who uses it. Anything else vulture reports is deleted.
* `tests/test_static_analysis.py` also runs ruff, mypy and vulture, and
  fails on: a Home Assistant import that is not public on the installed
  version, an `except` block that neither logs, raises nor reports (the
  short allowlist in that file records the reviewed exceptions), a
  translation key that is unused or missing, and an export kept only for
  tests.

## Mutation testing

`mutmut` mutates the safety-critical modules (`parsing.py`, `services.py`,
`controller.py`, `hybrid_inverter.py`, `notifications.py`; configured in
`pyproject.toml`) and runs the tests against each mutant. A surviving mutant
is a change to the code that no test notices.

```bash
rm -rf mutants                                      # a full run; incremental runs can keep stale results
.venv/bin/mutmut run --max-children 8               # about 40 minutes
.venv/bin/python tests/tools/classify_mutants.py   # survivors grouped by function, with their diffs
.venv/bin/python tests/tools/write_mutation_report.py
.venv/bin/python tests/tools/check_mutants.py
```

Kill a survivor with a test. If it cannot be killed (an equivalent mutant,
or only the wording of a message whose meaning the tests assert), add a rule
to `write_mutation_report.py` explaining why. The report generator fails on a
survivor no rule explains, and `check_mutants.py` (run weekly in CI) fails on
an unjustified survivor or a stale justification. Log calls are not mutated:
their wording is not behaviour, and that faults are logged is asserted
separately.

## Continuous integration

| Workflow | Runs | On |
| --- | --- | --- |
| `tests.yaml` | The suite with the coverage gate on Home Assistant 2026.3 and the latest test package; a non-blocking canary on the newest Home Assistant | push, pull request, weekly |
| `lint.yaml` | ruff and vulture; mypy on 2026.3 and latest | push, pull request |
| `hassfest.yaml` | Home Assistant's integration validator | push, pull request, daily |
| `validate.yaml` | The HACS validator | push, pull request, daily |
| `mutation.yaml` | A full mutation run and `check_mutants.py` | weekly, on demand |

## Changing the integration

* **Comments** state constraints the code cannot show; they do not narrate
  the next line or explain a change.
* **User-visible text** lives in `translations/en.json`; the static tests
  fail on a key the code uses that is missing, or one nothing uses.
* **Limits and thresholds** live in `const.py`, with the reason for the
  value.
* **A new entity**: declare it in `hybrid_inverter.py`, add its translation,
  its README row, and tests for its value, unknown and unavailable states.
* **A new work mode or field**: `services.py` (schema and fields),
  `services.yaml`, the translations, `device_action.py` selectors if it is a
  new field, the controller's sanitiser, and the README section; the cross
  cells then require its invalid-value tests.
* **A new repair issue**: `notifications.py`, its translations under
  `issues`, the README table, and tests that it is raised, cleared, and
  withdrawn on unload.

## Releasing

The Marketplace and HACS install from GitHub releases.

1. Bump `version` in `custom_components/eleven_energy_plus/manifest.json`.
2. Move the `Unreleased` section of `CHANGELOG.md` under the new version and
   date.
3. Commit, tag the commit `vX.Y.Z` (matching the manifest) and push the
   commit and the tag. Push only that tag: the clone also holds the
   upstream project's tags.
4. Create a GitHub release from the tag, marked latest.

## Upstream

This is a fork of [`iPeel/HA-Eleven-Energy`](https://github.com/iPeel/HA-Eleven-Energy).
[UPSTREAM.md](UPSTREAM.md) records, commit by commit, what was ported from
upstream and what was not, and the upstream commit last reviewed. When
upstream moves, review the new commits and extend that table.
