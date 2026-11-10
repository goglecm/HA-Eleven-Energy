"""Group mutmut's surviving mutants by what they change.

A helper for writing ``tests/mutation_report.md``: prints every mutant that
was not killed with the line it changes, grouped by function, so killing or
justifying them is a review of a list rather than one ``mutmut show`` at a
time. Run after ``mutmut run``.
"""

from __future__ import annotations

from collections import defaultdict
import contextlib
import io
import subprocess
import sys

from mutmut.__main__ import find_mutant, get_diff_for_mutant  # type: ignore[import-untyped]


def main() -> int:
    """Print the survivors, grouped by function, with their changed lines."""
    results = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    groups: dict[str, list[str]] = defaultdict(list)
    for line in results.splitlines():
        name, _, status = line.strip().partition(": ")
        if not name:
            continue
        with contextlib.redirect_stdout(io.StringIO()):
            mutant = find_mutant(name)
            diff = get_diff_for_mutant(name, path=mutant.path)
        changed = [row for row in diff.splitlines() if row.startswith(("-", "+")) and not row.startswith(("---", "+++"))]
        groups[name.rsplit("__mutmut_", 1)[0]].append(f"  {status} {name}\n    " + "\n    ".join(changed))
    for function, entries in sorted(groups.items()):
        print(f"{function} ({len(entries)})")
        print("\n".join(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
