"""Write ``tests/mutation_report.md`` from the results of ``mutmut run``.

Every mutant mutmut did not kill is listed with the reason it is not
killed by a test. Reasons come from :data:`RULES`, each a reviewed class of
mutant that cannot change observable behaviour, or that only changes the
wording of a message whose meaning (exception class, ``error_kind``) the
tests do assert. A survivor no rule explains makes this script fail: it must
then be killed with a test, or a reason added here after review.

Usage::

    mutmut run && python tests/tools/write_mutation_report.py
"""

from __future__ import annotations

from collections import Counter
import contextlib
from dataclasses import dataclass
import io
from pathlib import Path
import re
import subprocess
import sys

from mutmut.__main__ import find_mutant, get_diff_for_mutant  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "tests" / "mutation_report.md"
PACKAGE = "custom_components.eleven_energy_plus."


@dataclass(frozen=True)
class Rule:
    """A reviewed class of surviving mutant."""

    reason: str
    functions: tuple[str, ...]  # qualified function names (``""`` = any)
    removed: str  # regex the removed line must match
    added: str = ""  # regex the added line must match


_MESSAGE = (
    "Wording of a human-readable message. The tests assert what the failure "
    "is (exception class, `error_kind`), not its exact text."
)
_FALSY = (
    "Equivalent: the flag starts falsy either way (`False` and `None`) and is "
    "only ever tested for truth."
)
_OVERWRITTEN = "Equivalent: the initial value is always replaced before it is read."

RULES: tuple[Rule, ...] = (
    Rule(_MESSAGE, ("",), r'raise \w+Error\(|raise ConfigEntry\w+\(|ElevenEnergy\w+Error\("'),
    Rule(_MESSAGE, ("",), r'^\s*f?"[^"]*"\s*,?\s*$'),
    Rule(_MESSAGE, ("controller.Controller._fetch_site", "controller.Controller._fetch_device"), r"self\._rate_limited\("),
    Rule(_MESSAGE, ("controller.Controller.send_reliable_post",), r'error = "'),
    Rule(_FALSY, ("",), r"= False\s*$", r"= None\s*$"),
    Rule(_OVERWRITTEN, ("controller.Controller.__init__",), r"self\._(last_auth_check|polls_deferred_until|reported_interval)"),
    Rule(_OVERWRITTEN, ("hybrid_inverter.HybridInverter.__init__",), r"self\._seen_paths"),
    Rule(_OVERWRITTEN, ("controller.Controller.send_reliable_post",), r"retry_after: str \| None = None"),
    Rule(
        "Equivalent: HTTP methods and header names are case-insensitive (aiohttp "
        "upper-cases the method; response headers are a case-insensitive multidict).",
        ("controller.Controller._fetch_site", "controller.Controller._fetch_device", "controller.Controller._request"),
        r'"GET"|"Retry-After"',
    ),
    Rule(
        "Equivalent: only the debug log of the first device reading depends on it.",
        ("controller.Controller.__init__", "controller.Controller._apply_device_payload"),
        r"_logged_first_payload|_PAYLOAD_LOG_LIMIT",
    ),
    Rule(
        "Equivalent: a non-JSON body is only ever checked for being a dict (it is "
        "not one either way).",
        ("controller.Controller._request",),
        r"body = None",
    ),
    Rule(
        "Equivalent: command generations only need to differ from the newest; any "
        "start or step keeps them distinct.",
        ("controller.Controller._set_work_mode",),
        r"generation = self\._command_generations\.get",
    ),
    Rule(
        "Equivalent: clearing an issue does not use the inverter's name, and "
        "`False`/`None` both say the condition is absent.",
        ("controller.Controller._track_missing", "controller.Controller.set_work_mode"),
        r"report_(missing|offline)\(device_id, device\.display_name, False\)"
        r"|report_work_mode_result\(device_id, (self|owner)\._device_name\(device_id\), None\)",
    ),
    Rule(
        "Equivalent: `X` is an unreserved character, which `quote` never encodes, "
        "so adding it to `safe` changes nothing.",
        ("controller.device_path",),
        r"quote\(device_id, safe=''\)",
    ),
    Rule(
        "Equivalent: `None` and `False` both clear the credential notification.",
        ("controller.Controller._recover_auth", "notifications.FaultReporter.async_unload"),
        r"report_credential\(False\)",
    ),
    Rule(
        "Equivalent: every caller stops for the same condition straight after "
        "`poll_devices` returns (shutdown, a rejected credential, or a deferral that "
        "already counts as the API answering), so the return value is not read then.",
        ("controller.Controller.poll_devices",),
        r"break|reached = (True|False)",
    ),
    Rule(
        "Equivalent: the background task's name is only a label for debugging.",
        ("controller.Controller.start_poller",),
        r'"Eleven Energy Plus Poll"',
    ),
    Rule(
        "Equivalent: the poll task is cancelled first and ends at once, so the wait "
        "never runs into its timeout.",
        ("controller.Controller.terminate",),
        r"asyncio\.wait\(\{task\}, timeout=5\)",
    ),
    Rule(
        "Equivalent: without a known inverter the name is the fallback either way.",
        ("controller.Controller._device_name",),
        r"self\.devices\.get\(device_id\) if device_id is not None",
    ),
    Rule(
        "Equivalent: `hass` is only used to avoid entity-id collisions in "
        "`generate_entity_id`, and the ids are built from unique ids, which are "
        "already unique.",
        (
            "controller.Controller._discover",
            "hybrid_inverter.HybridInverter.__init__",
            "hybrid_inverter.InverterSensorEntity.__init__",
            "hybrid_inverter.InverterBinarySensorEntity.__init__",
            "hybrid_inverter._InverterEntityMixin._init_common",
        ),
        r"self\.hass,|self\.hass = hass|hass=hass|unique_id, \[\], hass",
    ),
    Rule(
        "Equivalent: a reading that is not an object is handled the same as none "
        "(every consumer checks for a dict).",
        ("hybrid_inverter.HybridInverter.__init__",),
        r"state if isinstance\(state, dict\) else None",
    ),
    Rule(
        "Equivalent: `rsplit` with no limit, or a larger one, gives the same last segment.",
        ("hybrid_inverter._infer_meta",),
        r'path\.rsplit\("\.", 1\)\[-1\]',
    ),
    Rule(
        "Equivalent: any negative number means \"no display precision\".",
        ("hybrid_inverter.HybridInverter._create_sensor",),
        r'meta\.get\("decimals", -1\)',
    ),
    Rule(
        "Equivalent: for `pv.mppts` the entity set always exists already (the index "
        "was checked against the model), so the creation branch cannot run.",
        ("hybrid_inverter.HybridInverter._process_indexed_lists",),
        r'list_path == "battery\.batteries" and not any\(',
    ),
    Rule(
        "Equivalent: all fields of an item's entity set exist or none do, so "
        "stopping at the first missing one skips the same fields.",
        ("hybrid_inverter.HybridInverter.positional_migrations",),
        r"continue",
    ),
    Rule(
        "Equivalent: an empty and a `None` root path both mean \"top level\".",
        ("hybrid_inverter.HybridInverter.update",),
        r'self\._walk\(payload, ""\)',
    ),
    Rule(
        "Equivalent: no binary sensor has translation placeholders.",
        ("hybrid_inverter.InverterBinarySensorEntity.__init__",),
        r"translation_placeholders=translation_placeholders",
    ),
    Rule(
        "Equivalent: `hass` is a class attribute of every entity (`None` until added), "
        "so the default is never used.",
        ("hybrid_inverter._InverterEntityMixin.write_state",),
        r'getattr\(self, "hass", None\)',
    ),
    Rule(
        "Equivalent: a restored reference of 0 accepts the same readings as no "
        "reference, and restoring an equal value changes nothing.",
        ("hybrid_inverter.TotalIncreasingGuard.restore",),
        r"value >= 0 and \(self\.reference is None or value > self\.reference\)",
    ),
    Rule(
        "Equivalent: removing a store does not depend on its schema version.",
        ("notifications.FaultReporter.async_remove",),
        r"STORAGE_VERSION",
    ),
)


def _function(name: str) -> str:
    qualified = name.removeprefix(PACKAGE).rsplit("__mutmut_", 1)[0]
    module, _, rest = qualified.partition(".")
    return f"{module}.{rest.removeprefix('x_').replace('xǁ', '').replace('ǁ', '.')}"


def _changed(name: str) -> tuple[list[str], list[str]]:
    with contextlib.redirect_stdout(io.StringIO()):
        mutant = find_mutant(name)
        diff = get_diff_for_mutant(name, path=mutant.path)
    rows = diff.splitlines()
    removed = [r[1:] for r in rows if r.startswith("-") and not r.startswith("---")]
    added = [r[1:] for r in rows if r.startswith("+") and not r.startswith("+++")]
    return removed, added


def _reason(function: str, removed: list[str], added: list[str]) -> str | None:
    for rule in RULES:
        if "" not in rule.functions and function not in rule.functions:
            continue
        if not any(re.search(rule.removed, line) for line in removed):
            continue
        if rule.added and not any(re.search(rule.added, line) for line in added):
            continue
        return rule.reason
    return None


def main() -> int:
    """Write the report; fail on an unexplained survivor."""
    results = subprocess.run(
        [sys.executable, "-m", "mutmut", "results", "--all", "true"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    statuses: dict[str, str] = {}
    for line in results.splitlines():
        name, _, status = line.strip().partition(": ")
        if name:
            statuses[name] = status
    counts = Counter(statuses.values())
    rows: list[str] = []
    unexplained: list[str] = []
    for name, status in sorted(statuses.items()):
        if status in ("killed", "timeout"):
            continue
        removed, added = _changed(name)
        function = _function(name)
        reason = _reason(function, removed, added)
        change = " / ".join(f"`{line.strip()}` → `{a.strip()}`" for line, a in zip(removed, added, strict=False)) or "(line removed)"
        if reason is None:
            unexplained.append(f"{name}: {change}")
            continue
        rows.append(f"| `{name}` | {change.replace('|', '\\|')} | {reason} |")
    total = sum(counts.values())
    detected = counts["killed"] + counts["timeout"]
    lines = [
        "# Mutation testing report",
        "",
        "Generated by `tests/tools/write_mutation_report.py` from `mutmut run` "
        "(mutmut 3.8.1, configuration in `pyproject.toml`) over the safety-critical "
        "modules: `parsing.py`, `services.py`, `controller.py`, `hybrid_inverter.py` "
        "and `notifications.py`.",
        "",
        f"- Mutants: {total}",
        f"- Killed: {counts['killed']}; timed out (the tests hung, so detected): {counts['timeout']}",
        f"- Survived: {total - detected}, every one justified below (equivalent "
        "mutants, or the wording of a message whose meaning is asserted)",
        f"- Mutation score: {100 * detected / total:.1f} %",
        "",
        "Log calls are not mutated (`do_not_mutate_patterns`): their wording is not "
        "behaviour, and that faults are logged is asserted where it matters - by "
        "`caplog` checks, and by the AST scan in `tests/test_static_analysis.py` that "
        "fails on any `except` block that neither logs, raises nor reports.",
        "",
        "## Justified survivors",
        "",
        "| Mutant | Change | Why no test kills it |",
        "| --- | --- | --- |",
        *rows,
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    for entry in unexplained:
        print(f"UNEXPLAINED {entry}")
    print(f"{total} mutants, {detected} detected, {len(rows)} justified, {len(unexplained)} unexplained")
    return 1 if unexplained else 0


if __name__ == "__main__":
    raise SystemExit(main())
