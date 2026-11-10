"""Run the test suite on a Home Assistant newer than the test package.

``pytest-homeassistant-custom-component`` publishes a release per Home
Assistant version, usually some days after Home Assistant itself. To get an
early warning, the CI canary job installs the newest Home Assistant over the
latest test package and runs the suite through this script, which applies
the test-harness shims that combination needs. Nothing here touches the
integration; every shim is a no-op when it isn't needed.

* Home Assistant 2026.11 aliases ``httpx`` to ``httpx2`` when it is imported,
  and refuses if ``httpx`` was imported first - which a pytest plugin does.
  Aliasing before pytest starts makes the later call a no-op.
* The 2026.10 test package imports ``_clear_logger_overwrites`` from the
  logger component, which 2026.11 moved to ``logger.helpers``.
"""

from __future__ import annotations

import sys


def _apply_shims() -> None:
    try:
        import httpx2  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        httpx2.alias_httpx()

    import homeassistant.components.logger as logger_component

    if not hasattr(logger_component, "_clear_logger_overwrites"):
        from homeassistant.components.logger.helpers import (  # type: ignore[attr-defined]
            _clear_logger_overwrites,
        )

        logger_component._clear_logger_overwrites = _clear_logger_overwrites  # type: ignore[attr-defined]


def main() -> int:
    """Apply the shims, then hand over to pytest."""
    _apply_shims()
    import pytest

    return pytest.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
