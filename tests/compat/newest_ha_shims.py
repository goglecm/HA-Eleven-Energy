"""Test-harness shims for a Home Assistant newer than the test package.

Loaded as a pytest plugin (``-p tests.compat.newest_ha_shims``) by
``run_newest_ha.py``. ``-p`` plugins are imported before any installed
plugin, and by every pytest-xdist worker too, so the shims are in place
wherever tests run. Nothing here touches the integration; every shim is a
no-op when it isn't needed.

* Home Assistant 2026.11 aliases ``httpx`` to ``httpx2`` when it is imported,
  and refuses if ``httpx`` was imported first - which a pytest plugin does.
  Aliasing before the other plugins load makes the later call a no-op.
* The 2026.10 test package imports ``_clear_logger_overwrites`` from the
  logger component, which 2026.11 moved to ``logger.helpers``.
"""

from __future__ import annotations


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


_apply_shims()
