"""Shared fixtures and reporting hooks for the Eleven Energy Plus test suite.

Besides the fixtures, this file hosts two reporting plugins:

* **Functional coverage** - collects the ``covers(...)`` markers of passing
  tests, checks them against ``tests/functional_coverage.py``, writes
  ``tests/functional_coverage_report.md``, and fails a full run that leaves
  any item or cross-coverage cell uncovered.
* **Defect report** - with ``--defect-report=PATH``, writes a JSON map of
  every ``defect(...)`` / ``guard(...)`` test to its outcome. Used by
  ``tests/tools/check_red.py`` to prove each defect test fails on the
  baseline commit.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Generator
import json
import logging
from pathlib import Path
import sys
from typing import Any
from unittest.mock import patch

import pytest

# Make ``custom_components.eleven_energy_plus`` importable as a top-level
# package so pytest-homeassistant-custom-component picks it up via its custom
# integration loader.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.functional_coverage import ALL_IDS, CROSS_CELLS, ITEMS  # noqa: E402

pytest_plugins = ["pytest_homeassistant_custom_component"]

TESTS_DIR = Path(__file__).resolve().parent
FUNCTIONAL_REPORT = TESTS_DIR / "functional_coverage_report.md"

# Messages Home Assistant logs when an integration misuses an API, or when a
# sensor misbehaves in ways that corrupt statistics:
# "Detected that custom integration ... This will stop working in ...",
# "Detected blocking call to open ... inside the event loop ...",
# "Entity ... from integration X has state class total_increasing, but its
# state is not strictly increasing ...", "... its state is negative ...".
# Any of these mentioning this integration fails the test that caused it.
_HA_USAGE_REPORT_MARKERS = (
    "Detected that custom integration",
    "Detected blocking call",
    "Detected code that",
    "will stop working",
    "is not strictly increasing",
    "its state is negative",
)


def is_integration_misuse(message: str) -> bool:
    """Return True for a Home Assistant misuse report about this integration."""
    return "eleven_energy_plus" in message and any(
        marker in message for marker in _HA_USAGE_REPORT_MARKERS
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Make the custom integration loader available to every test."""
    yield


@pytest.fixture(autouse=True)
def fail_on_ha_usage_reports(
    caplog: pytest.LogCaptureFixture,
) -> Generator[None]:
    """Fail any test during which Home Assistant reports misuse by this integration."""
    caplog.set_level(logging.WARNING, logger="homeassistant")
    yield
    offending = [
        record.getMessage()
        for when in ("setup", "call")
        for record in caplog.get_records(when)
        if record.levelno >= logging.WARNING and is_integration_misuse(record.getMessage())
    ]
    if offending:
        pytest.fail(
            "Home Assistant reported misuse by eleven_energy_plus:\n"
            + "\n".join(offending)
        )


@pytest.fixture
def instant_backoff() -> Generator[list[float]]:
    """Make POST retry backoff instantaneous; yields the requested delays."""
    delays: list[float] = []

    async def _no_wait(self, delay: float) -> None:
        delays.append(delay)

    with patch(
        "custom_components.eleven_energy_plus.controller.Controller._wait_backoff",
        _no_wait,
    ):
        yield delays


# ---------------------------------------------------------------------------
# Reporting plugins
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the reporting options."""
    group = parser.getgroup("eleven_energy_plus")
    group.addoption(
        "--defect-report",
        default=None,
        help="Write a JSON map of defect/guard tests to their outcome.",
    )
    group.addoption(
        "--functional-coverage",
        choices=("auto", "strict", "report", "off"),
        default="auto",
        help=(
            "auto: enforce on a full run, report otherwise; strict: always "
            "enforce; report: never enforce; off: do nothing."
        ),
    )


class _Collector:
    """State shared by the reporting hooks for one session."""

    def __init__(self) -> None:
        self.marks: dict[str, dict[str, list[str]]] = {}
        self.outcomes: dict[str, str] = {}
        self.longreprs: dict[str, str] = {}
        self.unknown_ids: dict[str, set[str]] = defaultdict(set)
        self.full_run = False


_COLLECTOR = _Collector()


def _is_full_run(config: pytest.Config) -> bool:
    """Return True when the whole suite is being run unfiltered."""
    if config.option.keyword or config.option.markexpr:
        return False
    if getattr(config.option, "lf", False) or getattr(config.option, "last_failed", False):
        return False
    args = [arg for arg in config.args if not arg.startswith("-")]
    if not args:
        return True
    return all(Path(arg).resolve() == TESTS_DIR for arg in args)


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Record the defect, guard and coverage markers of every collected test."""
    _COLLECTOR.full_run = _is_full_run(config) and len(items) > 0
    for item in items:
        marks: dict[str, list[str]] = {"defect": [], "guard": [], "covers": []}
        for name, values in marks.items():
            for mark in item.iter_markers(name):
                values.extend(str(arg) for arg in mark.args)
        for covered in marks["covers"]:
            if covered not in ALL_IDS:
                _COLLECTOR.unknown_ids[covered].add(item.nodeid)
        _COLLECTOR.marks[item.nodeid] = marks


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Track each test's overall outcome (any failed phase fails the test)."""
    previous = _COLLECTOR.outcomes.get(report.nodeid)
    if report.failed:
        _COLLECTOR.outcomes[report.nodeid] = "failed" if report.when == "call" else "error"
        _COLLECTOR.longreprs[report.nodeid] = str(report.longrepr)[-4000:]
    elif report.skipped and previous is None:
        _COLLECTOR.outcomes[report.nodeid] = "skipped"
    elif report.when == "call" and report.passed and previous is None:
        _COLLECTOR.outcomes[report.nodeid] = "passed"


def _write_functional_report(covered: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    uncovered_items = [item_id for item_id in ITEMS if not covered.get(item_id)]
    uncovered_cells = [cell for cell in CROSS_CELLS if not covered.get(cell)]
    lines = [
        "# Functional coverage report",
        "",
        "Generated by `tests/conftest.py` from the `covers(...)` markers of",
        "passing tests on the last full run. Model: `tests/functional_coverage.py`.",
        "",
        f"- Items covered: {len(ITEMS) - len(uncovered_items)} / {len(ITEMS)}",
        f"- Cross-coverage cells covered: "
        f"{len(CROSS_CELLS) - len(uncovered_cells)} / {len(CROSS_CELLS)}",
        "",
        "## Items",
        "",
        "| Id | Behaviour | Passing tests |",
        "| --- | --- | --- |",
    ]
    for item_id, description in ITEMS.items():
        count = len(covered.get(item_id, []))
        lines.append(
            f"| {item_id} | {description} | {count if count else '**UNCOVERED**'} |"
        )
    lines += ["", "## Cross-coverage cells", ""]
    groups: dict[str, list[str]] = defaultdict(list)
    for cell in CROSS_CELLS:
        groups[cell.split("-")[1]].append(cell)
    for group, cells in groups.items():
        hit = sum(1 for cell in cells if covered.get(cell))
        lines.append(f"- X-{group}: {hit} / {len(cells)} cells")
        lines.extend(
            f"  - **UNCOVERED** {cell}" for cell in cells if not covered.get(cell)
        )
    FUNCTIONAL_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return uncovered_items, uncovered_cells


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write the reports and apply the functional-coverage gate."""
    config = session.config
    defect_report = config.getoption("--defect-report")
    if defect_report:
        payload: dict[str, Any] = {}
        for nodeid, marks in _COLLECTOR.marks.items():
            if not (marks["defect"] or marks["guard"]):
                continue
            payload[nodeid] = {
                "defects": marks["defect"],
                "guards": marks["guard"],
                "outcome": _COLLECTOR.outcomes.get(nodeid, "not run"),
                "longrepr": _COLLECTOR.longreprs.get(nodeid, ""),
            }
        Path(defect_report).write_text(json.dumps(payload, indent=1), encoding="utf-8")

    mode = config.getoption("--functional-coverage")
    if mode == "off" or config.option.collectonly:
        return

    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if _COLLECTOR.unknown_ids:
        for unknown, nodeids in sorted(_COLLECTOR.unknown_ids.items()):
            message = f"covers({unknown!r}) is not in tests/functional_coverage.py ({sorted(nodeids)[0]})"
            if reporter:
                reporter.write_line(f"FUNCTIONAL COVERAGE: {message}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return

    enforce = mode == "strict" or (mode == "auto" and _COLLECTOR.full_run)
    if not (enforce or mode == "report"):
        return

    covered: dict[str, list[str]] = defaultdict(list)
    for nodeid, marks in _COLLECTOR.marks.items():
        if _COLLECTOR.outcomes.get(nodeid) == "passed":
            for item_id in marks["covers"]:
                covered[item_id].append(nodeid)
    uncovered_items, uncovered_cells = _write_functional_report(covered)
    if reporter:
        reporter.write_line(
            f"Functional coverage: {len(ITEMS) - len(uncovered_items)}/{len(ITEMS)} "
            f"items, {len(CROSS_CELLS) - len(uncovered_cells)}/{len(CROSS_CELLS)} "
            f"cross cells (report: {FUNCTIONAL_REPORT.relative_to(ROOT)})"
        )
    if enforce and (uncovered_items or uncovered_cells):
        if reporter:
            for item_id in uncovered_items + uncovered_cells:
                reporter.write_line(f"FUNCTIONAL COVERAGE: uncovered {item_id}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
