"""Fail when a mutant survived that ``tests/mutation_report.md`` does not justify.

Run after ``mutmut run``. Every mutant mutmut did not detect (``survived``,
``no tests``, ``suspicious``, ...; a timeout counts as detected) must be listed in the report's
"Justified survivors" table, with the reason it cannot be killed (an
equivalent mutant, typically). A listed mutant that no longer survives is
reported too, so the table does not go stale.

With ``--shard SHARD --of COUNT`` only the mutants of that shard
(``tests/tools/mutation_shards.py``) are compared, so each parallel job
checks what it ran.

Usage::

    mutmut run && python tests/tools/check_mutants.py
    python tests/tools/mutation_shards.py run 3 8 && python tests/tools/check_mutants.py --shard 3 --of 8
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

from mutation_shards import in_shard

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "tests" / "mutation_report.md"
_RESULT = re.compile(r"^\s+(\S+): (.+)$")
_JUSTIFIED_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def surviving_mutants() -> dict[str, str]:
    """Return ``{mutant: status}`` for every mutant that was not killed."""
    run = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    found: dict[str, str] = {}
    for line in run.stdout.splitlines():
        # A mutant whose tests time out was detected: the tests did not pass.
        if (match := _RESULT.match(line)) and match.group(2).strip() != "timeout":
            found[match.group(1)] = match.group(2).strip()
    return found


def justified_mutants() -> set[str]:
    """Return the mutants the report's justification table lists."""
    justified: set[str] = set()
    in_table = False
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_table = line.strip() == "## Justified survivors"
            continue
        if in_table and (match := _JUSTIFIED_ROW.match(line)):
            justified.add(match.group(1))
    return justified


def main() -> int:
    """Compare mutmut's survivors with the justified ones."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, default=None)
    parser.add_argument("--of", type=int, default=None, dest="count")
    args = parser.parse_args()
    if (args.shard is None) != (args.count is None):
        parser.error("--shard and --of go together")
    survivors = surviving_mutants()
    justified = justified_mutants()
    if args.shard is not None:
        survivors = {
            mutant: status
            for mutant, status in survivors.items()
            if in_shard(mutant, args.shard, args.count)
        }
        justified = {mutant for mutant in justified if in_shard(mutant, args.shard, args.count)}
    unjustified = sorted(set(survivors) - justified)
    stale = sorted(justified - set(survivors))
    for mutant in unjustified:
        print(f"UNJUSTIFIED {survivors[mutant]}: {mutant}")
    for mutant in stale:
        print(f"STALE (now killed, remove from the report): {mutant}")
    print(f"{len(survivors)} not killed, {len(justified)} justified")
    return 1 if unjustified or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
