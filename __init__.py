"""ET₀ Irrigation — Penman-Monteith FAO-56 deficit-based irrigation component."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .automation import async_create_automation, async_remove_automation
from .dashboard import async_create_dashboard, async_remove_dashboard
from .services import async_setup_services, async_unload_services
from .const import AUTOMATION_UNIQUE_ID_KEY

_LOGGER = logging.getLogger(__name__)

DOMAIN = "et0_irrigation"
PLATFORMS = ["sensor", "button"]


def _merged_entry_config(entry: ConfigEntry) -> dict:
    """Return runtime config by overlaying options on top of entry data."""
    merged = {**entry.data, **entry.options}
    merged.pop(AUTOMATION_UNIQUE_ID_KEY, None)
    return merged


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = _merged_entry_config(entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    try:
        await async_create_automation(hass, entry)
    except Exception:
        _LOGGER.exception(
            "ET₀ Irrigation: failed to create/reload automation; sensors will remain available"
        )

    try:
        await async_create_dashboard(hass, entry)
    except Exception:
        _LOGGER.exception(
            "ET₀ Irrigation: failed to create dashboard; integration will remain functional"
        )

    # Register services (idempotent — safe to call on every entry setup)
    await async_setup_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        await async_remove_automation(hass, entry)
    except Exception:
        _LOGGER.exception("ET₀ Irrigation: failed to remove/reload automation during unload")

    try:
        await async_remove_dashboard(hass, entry)
    except Exception:
        _LOGGER.exception("ET₀ Irrigation: failed to remove dashboard during unload")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    # Unload services only when no more entries remain
    await async_unload_services(hass)

    return unload_ok
