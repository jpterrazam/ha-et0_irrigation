"""Button entities for ET0 Irrigation."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ET0 Irrigation buttons."""
    async_add_entities([ResetZoneDeficitsButton(hass, entry)], update_before_add=False)


class ResetZoneDeficitsButton(ButtonEntity):
    """Reset all zone deficit sensors to zero on demand."""

    _attr_icon = "mdi:sprinkler-variant-off"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_name = "Reset Zone Deficits"
        self._attr_unique_id = f"et0_irrigation_reset_zone_deficits_{entry.entry_id}"

    async def async_press(self) -> None:
        """Handle button press by resetting every zone deficit sensor."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        zone_entities = entry_data.get("zone_deficit_entities", [])

        reset_count = 0
        for entity in list(zone_entities):
            reset_method = getattr(entity, "async_reset_deficit", None)
            if callable(reset_method):
                await reset_method()
                reset_count += 1

        _LOGGER.info(
            "ET0 Irrigation: reset button pressed, %d zone deficit sensor(s) reset",
            reset_count,
        )
