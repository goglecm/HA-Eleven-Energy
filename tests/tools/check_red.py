"""Prove every defect test fails on the commit that had the defect.

For each ledger entry in ``tests/DEFECTS.md`` there are tests marked
``@pytest.mark.defect("Dnn")``. A test that does not fail on the code that
had the defect does not catch the defect. Each entry's baseline column names
the commit to prove it against: ``fails`` means the default baseline
(``29b385d``, the last commit before the audit), ``fails at <sha>`` a later
commit (for a defect the audit itself introduced). For every such commit this
script:

1. creates a git worktree of the commit in a temporary directory;
2. copies the *current* ``tests/`` directory and the tool configuration
   (``pytest.ini``, ``pyproject.toml``, ``mypy.ini``, ``ruff.toml``,
   ``vulture_whitelist.py``) into it, so the current tests and rules run
   against the old integration code;
3. runs every ``defect`` and ``guard`` test there, with ``--defect-report``.

It then checks, per ledger entry, against the run for that entry's commit:

* the entry has at least one defect or guard test;
* for entries whose baseline column is ``fails`` / ``fails at <sha>``: it
  has a defect test, and every one of its defect tests failed there for a
  real reason - not an ``ImportError`` / missing attribute of the
  integration, which would only prove the test refers to code that did not
  exist yet;
* entries whose baseline column starts with ``n/a`` (nothing to fail
  against) are listed but not required to fail;
* every guard test passed there: a guard records a place where the old code
  was already right, so a failing guard is a mis-tagged defect.

Finally it writes ``tests/red_check_report.md`` and exits non-zero on any
violation, including a collection error on a baseline.

Usage::

    .venv/bin/python tests/tools/check_red.py [--baseline 29b385d]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests" / "DEFECTS.md"
REPORT = ROOT / "tests" / "red_check_report.md"

_IMPORT_FAILURE = re.compile(
    r"(ModuleNotFoundError|ImportError|cannot import name|has no attribute"
    r"|does not have the attribute|NameError)[^\n]*eleven_energy_plus"
    r"|fixture '[^']+' not found",
)
_CONFIG_FILES = (
    "pytest.ini",
    "pyproject.toml",
    "mypy.ini",
    "ruff.toml",
    "vulture_whitelist.py",
)
# pytest exit codes that mean the run itself broke (interrupted, internal
# error, usage error, no tests collected), as opposed to tests failing.
_BROKEN_RUN = {2, 3, 4, 5}


def parse_ledger() -> dict[str, str]:
    """Return ``{defect_id: baseline_expectation}`` from the ledger table."""
    defects: dict[str, str] = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 5 and re.fullmatch(r"D\d{2}", cells[0]):
            defects[cells[0]] = cells[4].lower()
    return defects


def baseline_of(expectation: str, default: str) -> str:
    """Return the commit an entry is proved against (``fails at <sha>``)."""
    match = re.fullmatch(r"fails at `?([0-9a-f]{7,40})`?", expectation)
    return match.group(1) if match else default


def _pytest_args(report: Path) -> list[str]:
    return [
        "-m",
        "pytest",
        "-q",
        "-m",
        "defect or guard",
        "-p",
        "no:randomly",
        "--tb=short",
        "--functional-coverage=off",
        f"--defect-report={report}",
    ]


def collect_current(python: str) -> dict[str, dict]:
    """Return the defect and guard tests of the current tree (collected only)."""
    with tempfile.TemporaryDirectory(prefix="ee-red-") as workdir:
        report = Path(workdir) / "current.json"
        run = subprocess.run(
            [python, *_pytest_args(report), "--collect-only"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if run.returncode != 0 or not report.exists():
            raise SystemExit(f"collecting the current tests failed:\n{run.stdout}{run.stderr}")
        return json.loads(report.read_text(encoding="utf-8"))


def run_baseline(baseline: str, python: str) -> dict[str, dict]:
    """Run the defect tests on ``baseline`` and return the defect report.

    Test modules that cannot even be imported against the baseline (they
    use names the fix introduced) are skipped by pytest; their tests are
    then absent from the report, which :func:`main` treats as an import
    failure rather than as red.
    """
    workdir = Path(tempfile.mkdtemp(prefix="ee-red-"))
    tree = workdir / "tree"
    subprocess.run(
        ["git", "-C", str(ROOT), "worktree", "add", "--detach", str(tree), baseline],
        check=True,
        capture_output=True,
    )
    try:
        shutil.rmtree(tree / "tests")
        shutil.copytree(
            ROOT / "tests",
            tree / "tests",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for name in _CONFIG_FILES:
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, tree / name)
        report = workdir / "defects.json"
        run = subprocess.run(
            [python, *_pytest_args(report), "--continue-on-collection-errors", "-n", "auto"],
            cwd=tree,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if run.returncode in _BROKEN_RUN or not report.exists():
            raise SystemExit(
                f"the baseline run broke (pytest exit code {run.returncode}); "
                "see the output above"
            )
        return json.loads(report.read_text(encoding="utf-8"))
    finally:
        subprocess.run(
            ["git", "-C", str(ROOT), "worktree", "remove", "--force", str(tree)],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(workdir, ignore_errors=True)


def complete(
    results: dict[str, dict], expected: dict[str, dict]
) -> tuple[dict[str, dict], list[str]]:
    """Account for current tests a baseline run could not have run.

    A test whose whole module did not import there, or a defect case that
    does not exist there, is recorded as an import failure (not red).
    Guards parametrised from the code itself (one case per import, say)
    have cases for new code that the baseline cannot have; they are
    returned separately, to be listed rather than counted.
    """
    results = dict(results)
    ran_modules = {nodeid.split("::")[0] for nodeid in results}
    not_applicable: list[str] = []
    for nodeid, info in expected.items():
        if nodeid in results:
            continue
        if nodeid.split("::")[0] not in ran_modules:
            results[nodeid] = {
                **info,
                "outcome": "error",
                "longrepr": "module not importable on the baseline: ImportError of eleven_energy_plus",
            }
        elif info["defects"]:
            results[nodeid] = {
                **info,
                "outcome": "error",
                "longrepr": "case does not exist on the baseline: NameError of eleven_energy_plus",
            }
        else:
            not_applicable.append(nodeid)
    return results, not_applicable


def main() -> int:
    """Run the check and write the report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="29b385d")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    ledger = parse_ledger()
    commit_of = {defect: baseline_of(exp, args.baseline) for defect, exp in ledger.items()}
    expected = collect_current(args.python)
    runs: dict[str, dict[str, dict]] = {}
    not_applicable: list[str] = []
    for commit in sorted(set(commit_of.values())):
        results, absent = complete(run_baseline(commit, args.python), expected)
        runs[commit] = results
        not_applicable += [
            f"`{nodeid}` (on `{commit}`)"
            for nodeid in absent
            if any(commit_of.get(guard) == commit for guard in expected[nodeid]["guards"])
        ]

    unknown = sorted(
        f"{nodeid} -> {defect}"
        for nodeid, info in expected.items()
        for defect in (*info["defects"], *info["guards"])
        if defect not in ledger
    )

    problems: list[str] = []
    lines = [
        "# Red check report",
        "",
        "Every `defect(...)` and `guard(...)` test run, with the current tests, "
        "against the commit that had the defect: "
        f"`{args.baseline}` (the code before the audit) unless the ledger says "
        "`fails at <sha>`. Defect tests must fail there; guard tests must pass.",
        "Generated by `tests/tools/check_red.py`.",
        "",
        "| Defect | Baseline expectation | Commit | Defect tests | Failed on baseline "
        "| Guard tests | Passed on baseline | Result |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for defect, expectation in ledger.items():
        results = runs[commit_of[defect]]
        tests = [(nodeid, info) for nodeid, info in results.items() if defect in info["defects"]]
        guards = [(nodeid, info) for nodeid, info in results.items() if defect in info["guards"]]
        failed = [
            nodeid
            for nodeid, info in tests
            if info["outcome"] in ("failed", "error")
            and not _IMPORT_FAILURE.search(info["longrepr"])
        ]
        guards_passed = [nodeid for nodeid, info in guards if info["outcome"] == "passed"]
        if not tests and not guards:
            result = "NO TESTS"
            problems.append(f"{defect}: no defect or guard tests")
        elif expectation.startswith("n/a"):
            result = "n/a (nothing to fail against)"
        elif not tests:
            result = "NO DEFECT TEST"
            problems.append(f"{defect}: baseline `fails` but no defect test")
        elif len(failed) == len(tests):
            result = "red as required"
        else:
            result = "NOT RED"
            for nodeid, info in tests:
                if nodeid not in failed:
                    reason = (
                        "import error" if _IMPORT_FAILURE.search(info["longrepr"])
                        else info["outcome"]
                    )
                    problems.append(f"{defect}: {nodeid} did not fail ({reason})")
        if len(guards_passed) != len(guards):
            result += "; GUARD FAILED"
            problems.extend(
                f"{defect}: guard {nodeid} failed on the baseline (mis-tagged defect?)"
                for nodeid, _info in guards
                if nodeid not in guards_passed
            )
        lines.append(
            f"| {defect} | {expectation} | `{commit_of[defect]}` | {len(tests)} | {len(failed)} "
            f"| {len(guards)} | {len(guards_passed)} | {result} |"
        )
    problems.extend(f"marker not in ledger: {entry}" for entry in unknown)

    lines += [
        "",
        "## Guard cases with no baseline counterpart",
        "",
        "Parametrised from code the audit added, so there is nothing on the "
        "baseline for them to check; they run against the fixed code only.",
        "",
    ]
    lines += [f"- {case}" for case in sorted(not_applicable)] or ["None."]
    lines += ["", "## Problems", ""]
    lines += [f"- {problem}" for problem in problems] or ["None."]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
