"""Split mutation testing into shards that run in parallel.

mutmut names every mutant after the function it changes:
``<module>.x_<function>__mutmut_<n>`` for a module-level function and
``<module>.xǁ<Class>ǁ<method>__mutmut_<n>`` for a method. This assigns each
function of the mutated modules (``[tool.mutmut] only_mutate`` in
``pyproject.toml``) to one of ``count`` shards, balancing them by an
estimate of each function's mutants, so that ``count`` jobs - each running
one shard - test every mutant exactly once. The assignment depends only on the source, so
every job computes the same one.

Usage::

    python tests/tools/mutation_shards.py patterns SHARD COUNT
    python tests/tools/mutation_shards.py run SHARD COUNT [--max-children N]

``patterns`` prints the shard's mutant name patterns; ``run`` runs
``mutmut run`` on them. ``SHARD`` counts from 1. Check the result with
``tests/tools/check_mutants.py --shard SHARD --of COUNT``.
"""

from __future__ import annotations

import argparse
import ast
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def _mutated_files() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    files: list[str] = config["tool"]["mutmut"]["only_mutate"]
    return files


def functions() -> list[tuple[str, int]]:
    """Return ``(mutant name pattern, weight)`` for every mutated function."""
    found: list[tuple[str, int]] = []
    for path in _mutated_files():
        module = path.removesuffix(".py").replace("/", ".")
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((f"{module}.x_{node.name}__mutmut_*", _weight(node)))
            elif isinstance(node, ast.ClassDef):
                found.extend(
                    (f"{module}.xǁ{node.name}ǁ{item.name}__mutmut_*", _weight(item))
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
    return found


def _weight(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Estimate how many mutants mutmut makes of a function.

    Counts what it mutates: constants, comparison and other operators, call
    arguments, assignments and returns. Line counts are a poor proxy - a
    dense table of comparisons yields several times more mutants per line.
    """
    count = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant | ast.BinOp | ast.UnaryOp | ast.AugAssign | ast.Assign | ast.AnnAssign | ast.Return):
            count += 1
        elif isinstance(sub, ast.Compare):
            count += len(sub.ops)
        elif isinstance(sub, ast.BoolOp):
            count += len(sub.values) - 1
        elif isinstance(sub, ast.Call):
            count += len(sub.args) + len(sub.keywords)
    return count


@cache
def shards(count: int) -> list[list[str]]:
    """Return the mutant name patterns of each of ``count`` shards.

    Heaviest functions first, each to the lightest shard so far (ties to
    the lower shard), which keeps the shards within one function of each
    other.
    """
    loads = [0] * count
    assigned: list[list[str]] = [[] for _ in range(count)]
    for pattern, weight in sorted(functions(), key=lambda item: (-item[1], item[0])):
        lightest = min(range(count), key=lambda index: (loads[index], index))
        loads[lightest] += weight
        assigned[lightest].append(pattern)
    return assigned


def in_shard(mutant: str, shard: int, count: int) -> bool:
    """Return True when ``mutant`` belongs to shard ``shard`` (from 1) of ``count``."""
    return any(fnmatchcase(mutant, pattern) for pattern in shards(count)[shard - 1])


def main() -> int:
    """Print or run one shard."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("patterns", "run"))
    parser.add_argument("shard", type=int)
    parser.add_argument("count", type=int)
    parser.add_argument("--max-children", type=int, default=None)
    args = parser.parse_args()
    if not 1 <= args.shard <= args.count:
        parser.error("SHARD must be between 1 and COUNT")
    patterns = shards(args.count)[args.shard - 1]
    if args.action == "patterns":
        print("\n".join(patterns))
        return 0
    command = [sys.executable, "-m", "mutmut", "run"]
    if args.max_children is not None:
        command += ["--max-children", str(args.max_children)]
    return subprocess.run([*command, *patterns], cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
