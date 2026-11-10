"""Run the test suite on a Home Assistant newer than the test package.

``pytest-homeassistant-custom-component`` publishes a release per Home
Assistant version, usually some days after Home Assistant itself. To get an
early warning, the CI canary job installs the newest Home Assistant over the
latest test package and runs the suite through this script, which loads the
test-harness shims that combination needs (``newest_ha_shims.py``) as a
pytest plugin. Arguments are passed to pytest, ``-n`` included: the plugin is
loaded by every worker.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """Hand over to pytest with the shims plugin, importable in every process."""
    os.environ["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT), os.environ.get("PYTHONPATH", "")) if part
    )
    sys.path.insert(0, str(ROOT))
    import pytest

    return pytest.main(["-p", "tests.compat.newest_ha_shims", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
