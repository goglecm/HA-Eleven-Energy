"""Static checks: typing, lint, imports, translations, silent failures, ledger.

These catch defects that only show up as a Home Assistant upgrade breaking
the integration (non-public imports), as untranslated or dead UI strings, as
errors swallowed without a trace, or as lint/typing rot.
"""

from __future__ import annotations

import ast
from functools import cache
import importlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest
import yaml

import custom_components.eleven_energy_plus as package

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "custom_components" / "eleven_energy_plus"
TESTS = ROOT / "tests"
LEDGER = TESTS / "DEFECTS.md"


def _modules() -> list[Path]:
    return sorted(PACKAGE.glob("*.py"))


def _tool(name: str) -> str:
    """Return the path of a CLI tool next to the running interpreter."""
    candidate = Path(sys.executable).with_name(name)
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        pytest.skip(f"{name} is not installed")
    return found


# ---------------------------------------------------------------------------
# Strict typing and lint: D15, D17, D25, D27, D28
# ---------------------------------------------------------------------------


@cache
def _mypy_output() -> tuple[int, str]:
    result = subprocess.run(
        [
            _tool("mypy"),
            "--config-file",
            str(ROOT / "mypy.ini"),
            "--cache-dir",
            "/tmp/ee-mypy-cache",
            str(PACKAGE),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


_MYPY_CHECKS = {
    "D15": re.compile(r"hybrid_inverter\.py.*\[(misc|assignment)\]"),
    "D17": re.compile(r"(Schema|schema).*\[arg-type\]"),
    "D25": re.compile(r"const\.py.*\[unused-ignore\]"),
}


@pytest.mark.parametrize(
    "defect",
    [
        pytest.param(defect, id=defect, marks=pytest.mark.defect(defect))
        for defect in _MYPY_CHECKS
    ],
)
def test_strict_typing_has_no_errors_of_this_kind(defect: str) -> None:
    _code, output = _mypy_output()
    matches = [line for line in output.splitlines() if _MYPY_CHECKS[defect].search(line)]
    assert not matches, "\n".join(matches)


@pytest.mark.covers("FC-STATIC-01")
def test_strict_typing_passes() -> None:
    code, output = _mypy_output()
    assert code == 0, output


# ---------------------------------------------------------------------------
# The README's behavioural statements map to the functional model
# ---------------------------------------------------------------------------

_BEHAVIOURAL_SECTIONS = (
    "## Connecting to Eleven Energy",
    "## Options",
    "### Availability and values you can trust",
    "## Repairs and notifications",
    "## Work modes",
)
_FC_TAG = re.compile(r"<!--\s*((?:FC-[A-Z]+-\d+\s*)+)-->")


def _readme_blocks(heading: str) -> list[str]:
    """Paragraphs, list items and quotes of a README section (not tables or code)."""
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(heading) + 1
    level = len(heading) - len(heading.lstrip("#"))
    # "Work modes" is checked up to its first sub-section (the per-mode
    # reference); every other section up to the next heading of its level.
    stop_level = 6 if heading == "## Work modes" else level
    blocks: list[str] = []
    current: list[str] = []
    in_code = False
    for line in lines[start:]:
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= stop_level:
            break
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.startswith("|"):
            continue
        if not line.strip() or line.startswith(("* ", "- ")):
            if current:
                blocks.append(" ".join(current))
            current = [line] if line.strip() else []
            continue
        current.append(line)
    if current:
        blocks.append(" ".join(current))
    return [block for block in blocks if block.strip()]


@pytest.mark.covers("FC-STATIC-06")
@pytest.mark.parametrize("heading", _BEHAVIOURAL_SECTIONS)
def test_every_readme_behaviour_maps_to_the_model(heading: str) -> None:
    from .functional_coverage import ITEMS

    untagged = [block[:80] for block in _readme_blocks(heading) if not _FC_TAG.search(block)]
    assert not untagged, f"statements without an FC tag in {heading!r}: {untagged}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tags = {tag for match in _FC_TAG.finditer(readme) for tag in match.group(1).split()}
    assert tags <= set(ITEMS), sorted(tags - set(ITEMS))


def test_there_is_no_dead_code() -> None:
    """Everything vulture reports is either deleted or a reviewed whitelist entry."""
    result = subprocess.run(
        [
            _tool("vulture"),
            "custom_components",
            "tests",
            "vulture_whitelist.py",
            "--min-confidence",
            "60",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.defect("D27")
def test_ruff_is_clean() -> None:
    result = subprocess.run(
        [_tool("ruff"), "check", "--no-cache", "custom_components", "tests"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Home Assistant imports must be public (the upstream 2026.11 ImportError class)
# ---------------------------------------------------------------------------


def _ha_imports() -> list[tuple[str, str, str]]:
    found = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "homeassistant"
            ):
                found.extend((path.name, node.module, alias.name) for alias in node.names)
    return sorted(set(found))


def _public_names(module_name: str) -> set[str]:
    """Names a module defines, lists in ``__all__`` or deliberately re-exports."""
    module = importlib.import_module(module_name)
    names = set(getattr(module, "__all__", ()))
    source_file = getattr(module, "__file__", None)
    if not source_file:
        return names | set(vars(module))
    source = Path(source_file).read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.TypeAlias)):
            target = node.target if isinstance(node, ast.AnnAssign) else node.name
            if isinstance(target, ast.Name):
                names.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            text = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            for alias in node.names:
                if alias.asname == alias.name or "noqa: F401" in text:
                    names.add(alias.asname or alias.name)
    return names


@pytest.mark.guard("D28")
@pytest.mark.covers("FC-STATIC-03")
@pytest.mark.parametrize(("source", "module", "name"), _ha_imports(), ids=str)
def test_home_assistant_import_is_public(source: str, module: str, name: str) -> None:
    imported = importlib.import_module(module)
    if hasattr(imported, "__path__") and importlib.util.find_spec(f"{module}.{name}"):
        return  # importing a submodule
    assert name in _public_names(module), (
        f"{source} imports {name} from {module}, which neither defines nor "
        "explicitly exports it - a future Home Assistant can drop it"
    )


# ---------------------------------------------------------------------------
# No silent except blocks (D31, D32 and the "silent" class)
# ---------------------------------------------------------------------------

# Handlers that intentionally do not log, each with the reason. Key:
# (module, enclosing function, exception names).
_SILENT_ALLOWED: dict[tuple[str, str, str], str] = {
    ("controller.py", "_request", "aiohttp.ContentTypeError,ValueError"): "a non-JSON body becomes None and the caller reports an unexpected payload",
    ("controller.py", "poll_site", "ElevenEnergyAuthError"): "reported where it is raised (_handle_auth_failure)",
    ("controller.py", "poll_site", "ElevenEnergyRateLimitedError"): "_defer_polls logs the deferral",
    ("controller.py", "poll_devices", "ElevenEnergyAuthError"): "reported where it is raised (_handle_auth_failure)",
    ("parsing.py", "finite_float", "TypeError,ValueError,OverflowError"): "parser: None means invalid and the caller reports it",
    ("parsing.py", "parse_retry_after", "TypeError,ValueError,IndexError,OverflowError"): "parser: None means no usable hint and the caller uses its own delay",
    ("config_flow.py", "_async_credential_error", "InvalidAuth"): "returned as a form error or abort reason shown to the user",
    ("config_flow.py", "_async_credential_error", "CannotConnect"): "returned as a form error or abort reason shown to the user",
}


def _handler_reports(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else ""
            if owner == "_LOGGER" or name.startswith(("_warn", "warn", "_report", "report", "_handle_", "record_")):
                return True
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                text = ast.unparse(target)
                if text in ("error", "errors") or text.startswith(("errors[", "result[")):
                    return True
    return False


def _handlers() -> list[tuple[str, str, str, int, bool]]:
    found = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            scope: ast.AST | None = node
            while scope is not None and not isinstance(
                scope, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                scope = parents.get(scope)
            function = scope.name if scope is not None else "<module>"
            if node.type is None:
                exc = "<bare>"
            elif isinstance(node.type, ast.Tuple):
                exc = ",".join(ast.unparse(elt) for elt in node.type.elts)
            else:
                exc = ast.unparse(node.type)
            found.append((path.name, function, exc, node.lineno, _handler_reports(node)))
    return found


@pytest.mark.defect("D31")
@pytest.mark.defect("D32")
@pytest.mark.covers("FC-STATIC-05")
def test_no_except_block_swallows_an_error_silently() -> None:
    silent = [
        f"{module}:{line} in {function}() catches {exc} without logging, raising or reporting"
        for module, function, exc, line, reports in _handlers()
        if not reports and (module, function, exc) not in _SILENT_ALLOWED
    ]
    assert not silent, "\n".join(silent)


def test_silent_allowlist_has_no_stale_entries() -> None:
    present = {
        (module, function, exc)
        for module, function, exc, _line, reports in _handlers()
        if not reports
    }
    stale = [key for key in _SILENT_ALLOWED if key not in present]
    assert not stale, stale


# ---------------------------------------------------------------------------
# Translations: no unused keys, nothing referenced but missing (D26)
# ---------------------------------------------------------------------------

# Keys Home Assistant itself emits for this integration (OAuth helper, single
# config entry, update-and-abort helpers, config-entry exceptions).
_HA_EMITTED_ABORTS = {
    "missing_configuration",
    "missing_credentials",
    "authorize_url_timeout",
    "no_url_available",
    "oauth_error",
    "oauth_failed",
    "oauth_timeout",
    "oauth_unauthorized",
    "oauth_implementation_unavailable",
    "user_rejected_authorize",
    "single_instance_allowed",
    "reauth_successful",
    "reconfigure_successful",
}


def _tree(name: str) -> ast.Module:
    return ast.parse((PACKAGE / name).read_text(encoding="utf-8"))


def _function(tree: ast.AST, name: str) -> ast.AST:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _returned_literals(node: ast.AST) -> set[str]:
    return {
        ret.value.value
        for ret in ast.walk(node)
        if isinstance(ret, ast.Return)
        and isinstance(ret.value, ast.Constant)
        and isinstance(ret.value.value, str)
    }


def _assigned_error_literals(node: ast.AST) -> set[str]:
    """Literals assigned as ``errors[...] = "key"``."""
    return {
        assign.value.value
        for assign in ast.walk(node)
        if isinstance(assign, ast.Assign)
        and isinstance(assign.value, ast.Constant)
        and any(
            isinstance(target, ast.Subscript) and ast.unparse(target.value) == "errors"
            for target in assign.targets
        )
    }


def _call_arg_literals(tree: ast.AST, method: str, position: int) -> set[str]:
    """Literal string arguments at ``position`` of every call to ``method``."""
    found = set()
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == method and len(call.args) > position:
            arg = call.args[position]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    return found


def _keyword_literals(tree: ast.AST, keyword: str) -> set[str]:
    return {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == keyword
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _used_translation_keys() -> dict[str, set[str]]:
    """Map each translation section to the keys the code can emit there."""
    flow = _tree("config_flow.py")
    options_flow = next(
        node for node in ast.walk(flow) if isinstance(node, ast.ClassDef) and node.name == "OptionsFlowHandler"
    )
    credential_outcomes = _returned_literals(_function(flow, "_async_credential_error"))
    services = _tree("services.py")
    raised = {
        elt.elts[1].value
        for node in ast.walk(services)
        if isinstance(node, ast.Dict)
        for elt in node.values
        if isinstance(elt, ast.Tuple)
        and len(elt.elts) == 2
        and isinstance(elt.elts[1], ast.Constant)
    }
    notifications = _tree("notifications.py")
    notification_keys = (
        _call_arg_literals(notifications, "raise_issue", 1)
        | _call_arg_literals(notifications, "_lasting", 3)
        | {
            node.value.value
            for node in notifications.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "CREDENTIAL_NOTIFICATION" for t in node.targets)
            and isinstance(node.value, ast.Constant)
        }
    )
    keyword_keys = set().union(
        *(_keyword_literals(ast.parse(path.read_text(encoding="utf-8")), "translation_key") for path in _modules())
    )
    # Steps that render something: ``step_id=`` literals, the step ids handed
    # to the shared token step, and Home Assistant's implementation picker.
    shown_steps = (
        _keyword_literals(flow, "step_id") | _call_arg_literals(flow, "_async_token_step", 0)
    ) - {"init"}
    return {
        # A token's outcome is a form error; a sign-in's ends the flow.
        "config.error": credential_outcomes,
        "config.abort": credential_outcomes | _HA_EMITTED_ABORTS,
        "config.step": shown_steps | {"pick_implementation"},
        "options.error": _assigned_error_literals(options_flow),
        "options.step": _keyword_literals(options_flow, "step_id"),
        "exceptions": raised | (keyword_keys & set(_strings().get("exceptions", {}))),
        "issues": notification_keys,
    }


def _strings() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (PACKAGE / "translations" / "en.json").read_text(encoding="utf-8")
    )
    return loaded


def _section(strings: dict[str, Any], dotted: str) -> dict[str, Any]:
    node: Any = strings
    for part in dotted.split("."):
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _translation_findings() -> tuple[list[str], list[str]]:
    """Return ``(unused, missing)`` translation keys."""
    strings = _strings()
    used = _used_translation_keys()
    unused = [
        f"{section}.{key}"
        for section, keys in used.items()
        for key in _section(strings, section)
        if key not in keys
    ]
    missing = [
        f"{section}.{key}"
        for section, keys in used.items()
        if section != "config.abort"
        for key in sorted(keys)
        if key not in _section(strings, section)
    ]
    missing += [
        f"config.abort.{key}"
        for key in sorted(used["config.abort"] - _HA_EMITTED_ABORTS)
        if key not in _section(strings, "config.abort")
    ]
    services = yaml.safe_load((PACKAGE / "services.yaml").read_text(encoding="utf-8"))
    unused += [f"services.{name}" for name in strings.get("services", {}) if name not in services]
    entity_sections = list(strings.get("entity", {}).values())
    missing += [
        f"translation_key={key!r}"
        for key in sorted(
            set().union(
                *(_keyword_literals(ast.parse(p.read_text(encoding="utf-8")), "translation_key") for p in _modules())
            )
        )
        if key not in strings.get("exceptions", {})
        and not any(key in section for section in entity_sections)
    ]
    return unused, missing


@pytest.mark.defect("D26")
@pytest.mark.covers("FC-STATIC-02")
def test_translations_have_no_unused_keys() -> None:
    unused, _missing = _translation_findings()
    assert not unused, unused


@pytest.mark.covers("FC-STATIC-02")
def test_translations_have_no_missing_keys() -> None:
    _unused, missing = _translation_findings()
    assert not missing, missing


# ---------------------------------------------------------------------------
# Dead exports (D22)
# ---------------------------------------------------------------------------


@pytest.mark.defect("D22")
def test_package_exports_nothing_only_for_tests() -> None:
    assert "SERVICE_NAMES" not in getattr(package, "__all__", ()), (
        "SERVICE_NAMES lives in services.py; the package re-export existed only for tests"
    )


# ---------------------------------------------------------------------------
# The misuse guard in conftest.py (D29)
# ---------------------------------------------------------------------------

_MISUSE_MESSAGES = {
    "deprecated_api": (
        "Detected that custom integration 'eleven_energy_plus' uses X. This will stop working in 2027.1",
        True,
    ),
    "blocking_call": (
        "Detected blocking call to open with args ... inside the event loop by custom integration 'eleven_energy_plus'",
        True,
    ),
    "not_strictly_increasing": (
        "Entity sensor.abc_pv_energy_today from integration eleven_energy_plus has state class "
        "total_increasing, but its state is not strictly increasing.",
        True,
    ),
    "negative": (
        "Entity sensor.abc_pv_energy_today from integration eleven_energy_plus has state class "
        "total_increasing, but its state is negative.",
        True,
    ),
    "other_integration": (
        "Detected that custom integration 'other' uses X. This will stop working in 2027.1",
        False,
    ),
    "ordinary_warning": ("Eleven Energy Plus site poll failed: HTTP 500 (eleven_energy_plus)", False),
}


@pytest.mark.guard("D29")
@pytest.mark.parametrize(("message", "misuse"), _MISUSE_MESSAGES.values(), ids=list(_MISUSE_MESSAGES))
def test_misuse_guard_recognises_every_report_kind(message: str, misuse: bool) -> None:
    from .conftest import is_integration_misuse

    assert is_integration_misuse(message) is misuse


@pytest.mark.guard("D43")
@pytest.mark.covers("FC-STATIC-06")
def test_parallel_workers_hand_their_markers_to_the_controller() -> None:
    from .conftest import _Collector, merge_worker_output, worker_output

    first, second = _Collector(), _Collector()
    first.full_run = second.full_run = True
    first.marks = {"t::a": {"defect": [], "guard": [], "covers": ["FC-CMD-01"]}}
    second.marks = {"t::b": {"defect": ["D01"], "guard": [], "covers": []}}
    second.unknown_ids["FC-NOPE-01"].add("t::b")

    controller = _Collector()
    for worker in (first, second):
        merge_worker_output(controller, worker_output(worker))
    assert controller.full_run, "the controller collects nothing of its own"
    assert controller.marks == {**first.marks, **second.marks}
    assert controller.unknown_ids == {"FC-NOPE-01": {"t::b"}}


@pytest.mark.guard("D43")
@pytest.mark.covers("FC-STATIC-06")
def test_a_parallel_worker_neither_writes_reports_nor_applies_the_gate(tmp_path: Path) -> None:
    from . import conftest

    class _Config:
        def __init__(self) -> None:
            self.workerinput: dict[str, Any] = {}
            self.workeroutput: dict[str, Any] = {}

        def getoption(self, name: str) -> Any:
            return {"--defect-report": str(tmp_path / "defects.json"), "--functional-coverage": "strict"}[name]

    class _Session:
        config = _Config()
        exitstatus = 0

    before = conftest.FUNCTIONAL_REPORT.read_text(encoding="utf-8")
    session = _Session()
    conftest.pytest_sessionfinish(session, 0)  # type: ignore[arg-type]
    assert session.exitstatus == 0
    assert not (tmp_path / "defects.json").exists()
    assert conftest.FUNCTIONAL_REPORT.read_text(encoding="utf-8") == before
    assert set(session.config.workeroutput) == {conftest.WORKER_OUTPUT_KEY}


# ---------------------------------------------------------------------------
# Ledger consistency
# ---------------------------------------------------------------------------


def _ledger_ids() -> dict[str, str]:
    ids = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 5 and re.fullmatch(r"D\d{2}", cells[0]):
            ids[cells[0]] = cells[4]
    return ids


def test_ledger_and_markers_agree(request: pytest.FixtureRequest) -> None:
    ledger = _ledger_ids()
    tagged: dict[str, int] = {}
    collected_files = set()
    for item in request.session.items:
        collected_files.add(Path(str(item.fspath)).name)
        for name in ("defect", "guard"):
            for mark in item.iter_markers(name):
                for defect in mark.args:
                    tagged[defect] = tagged.get(defect, 0) + 1
    unknown = sorted(set(tagged) - set(ledger))
    assert not unknown, f"markers for defects not in tests/DEFECTS.md: {unknown}"
    test_files = {path.name for path in TESTS.glob("test_*.py")}
    if test_files <= collected_files:  # full collection
        untested = sorted(set(ledger) - set(tagged))
        assert not untested, f"ledger defects without a tagged test: {untested}"


def _ledger_rows() -> list[list[str]]:
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 8 and re.fullmatch(r"D\d{2}", cells[0]):
            rows.append(cells)
    return rows


def test_ledger_rows_are_complete() -> None:
    """Every entry records its tests and its red and green evidence."""
    incomplete = [
        row[0]
        for row in _ledger_rows()
        if not all(row[index] for index in (5, 6, 7))
    ]
    assert not incomplete, f"ledger rows missing tests or evidence: {incomplete}"
