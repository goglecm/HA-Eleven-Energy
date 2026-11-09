"""Entity-registry migration from the pre-2.0 auto-discovered entities.

Before 2.0 the generic payload walker auto-discovered list items by their
*position* (``pv_mppts_0_power``), and it created entities for registers the
hardware does not have: a third MPPT on a two-tracker model, phase registers
on a single-phase machine, and the ``model`` block's own flags.

2.0 replaces those with curated entities keyed by each item's own ``index``
and chosen from the model (see :mod:`.hybrid_inverter`). This module moves
the registry entries - and with them the entity id and its history - onto
the curated entities where an equivalent exists, and removes the ones that
no longer correspond to anything.

It runs during ``async_setup_entry``, after discovery and before the
platforms add any entity, because the registry decides an entity's id and
it has to be changed while nothing is holding it. It is idempotent: a second
run finds nothing left to do.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)


@callback
def async_migrate_entity_registry(
    hass: HomeAssistant, entry: ConfigEntry, controller: Controller
) -> tuple[int, int]:
    """Migrate positional entities and drop obsolete ones.

    Returns ``(migrated, removed)`` counts, mainly for tests and logging.
    Nothing is removed for an inverter whose hardware is not known (no
    ``model`` block or no discovery reading), so a temporarily unreachable
    API can never cost anybody an entity.
    """
    ent_reg = er.async_get(hass)
    by_key: dict[tuple[str, str], er.RegistryEntry] = {
        (reg_entry.domain, reg_entry.unique_id): reg_entry
        for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if reg_entry.platform == DOMAIN
    }

    migrated = 0
    removed = 0

    for device_id, inverter in controller.devices.items():
        prefix = f"{device_id}_"

        for old_type, new_type in inverter.positional_migrations().items():
            old_key = ("sensor", prefix + old_type)
            new_key = ("sensor", prefix + new_type)
            old_entry = by_key.get(old_key)
            if old_entry is None or new_key in by_key:
                continue
            ent_reg.async_update_entity(
                old_entry.entity_id, new_unique_id=new_key[1]
            )
            by_key[new_key] = by_key.pop(old_key)
            migrated += 1
            _LOGGER.info(
                "Eleven Energy Plus moved %s from %s to %s",
                old_entry.entity_id,
                old_type,
                new_type,
            )

        patterns = inverter.obsolete_entity_patterns()
        if not patterns:
            continue
        curated = inverter.curated_entity_types
        for key, reg_entry in list(by_key.items()):
            unique_id = key[1]
            if not unique_id.startswith(prefix):
                continue
            entity_type = unique_id[len(prefix) :]
            if entity_type in curated:
                continue
            if any(pattern.match(entity_type) for pattern in patterns):
                ent_reg.async_remove(reg_entry.entity_id)
                del by_key[key]
                removed += 1
                _LOGGER.info(
                    "Eleven Energy Plus removed %s: superseded by a curated "
                    "entity or not present on this hardware",
                    reg_entry.entity_id,
                )

    return migrated, removed
