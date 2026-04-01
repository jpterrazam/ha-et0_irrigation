"""Services for ET₀ Irrigation — zone parameter editing."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .automation import async_cleanup_automation_ghosts
from .const import (
    CONF_ZONE_APPLICATION_RATE,
    CONF_ZONE_FACTOR,
    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION,
    CONF_ZONE_MAX_MINUTES,
    CONF_ZONE_MIN_MINUTES,
    CONF_ZONE_SWITCH,
    CONF_ZONES,
    DEFAULT_APPLICATION_RATE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_ZONE_PARAMETER = "set_zone_parameter"
SERVICE_CLEANUP_AUTOMATION_GHOSTS = "cleanup_automation_ghosts"

SCHEMA_SET_ZONE_PARAMETER = vol.Schema(
    {
        vol.Required("zone_switch"): cv.string,
        vol.Optional("factor"): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=2.0)
        ),
        vol.Optional("min_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=120)
        ),
        vol.Optional("max_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=240)
        ),
        vol.Optional("application_rate"): vol.All(
            vol.Coerce(float), vol.Range(min=0.01, max=10.0)
        ),
        vol.Optional("max_days_without_irrigation"): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=5)
        ),
    }
)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register ET₀ Irrigation services."""

    async def _handle_set_zone_parameter(call: ServiceCall) -> None:
        """Update one or more parameters for a specific zone across all entries."""
        zone_switch = call.data["zone_switch"]

        # Find the config entry that owns this zone switch
        target_entry = None
        target_zone_idx = None

        for entry_id, config in hass.data.get(DOMAIN, {}).items():
            zones: list[dict] = config.get(CONF_ZONES, [])
            for idx, zone in enumerate(zones):
                if isinstance(zone, dict) and zone.get(CONF_ZONE_SWITCH) == zone_switch:
                    target_entry = hass.config_entries.async_get_entry(entry_id)
                    target_zone_idx = idx
                    break
            if target_entry is not None:
                break

        if target_entry is None:
            raise ServiceValidationError(
                f"ET₀ Irrigation: zone switch '{zone_switch}' not found in any config entry"
            )

        # Build updated zones list
        current_options = {**target_entry.data, **target_entry.options}
        zones = list(current_options.get(CONF_ZONES, []))
        zone = dict(zones[target_zone_idx])

        changed: dict[str, Any] = {}

        if "factor" in call.data:
            zone[CONF_ZONE_FACTOR] = round(call.data["factor"], 2)
            changed["factor"] = zone[CONF_ZONE_FACTOR]

        if "min_minutes" in call.data:
            zone[CONF_ZONE_MIN_MINUTES] = call.data["min_minutes"]
            changed["min_minutes"] = zone[CONF_ZONE_MIN_MINUTES]

        if "max_minutes" in call.data:
            zone[CONF_ZONE_MAX_MINUTES] = call.data["max_minutes"]
            changed["max_minutes"] = zone[CONF_ZONE_MAX_MINUTES]

        if "application_rate" in call.data:
            zone[CONF_ZONE_APPLICATION_RATE] = round(call.data["application_rate"], 4)
            changed["application_rate"] = zone[CONF_ZONE_APPLICATION_RATE]

        if "max_days_without_irrigation" in call.data:
            zone[CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION] = call.data["max_days_without_irrigation"]
            changed["max_days_without_irrigation"] = zone[CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION]

        if not changed:
            _LOGGER.warning(
                "ET₀ Irrigation set_zone_parameter: no parameters provided for zone '%s'",
                zone_switch,
            )
            return

        zones[target_zone_idx] = zone

        # Persist via options update — this triggers _async_update_listener → reload
        new_options = {**target_entry.options, CONF_ZONES: zones}
        hass.config_entries.async_update_entry(target_entry, options=new_options)

        _LOGGER.info(
            "ET₀ Irrigation: zone '%s' updated: %s",
            zone_switch,
            ", ".join(f"{k}={v}" for k, v in changed.items()),
        )

    async def _handle_cleanup_automation_ghosts(call: ServiceCall) -> None:
        """Remove stale ET0 automation entities that may pollute selectors."""
        await async_cleanup_automation_ghosts(hass)
        _LOGGER.info("ET₀ Irrigation: cleanup_automation_ghosts executed")

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ZONE_PARAMETER,
        _handle_set_zone_parameter,
        schema=SCHEMA_SET_ZONE_PARAMETER,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEANUP_AUTOMATION_GHOSTS,
        _handle_cleanup_automation_ghosts,
    )
    _LOGGER.debug("ET₀ Irrigation: services registered")


async def async_unload_services(hass: HomeAssistant) -> None:
    """Remove ET₀ Irrigation services when last entry is unloaded."""
    # Only remove services when no more entries are loaded
    if hass.data.get(DOMAIN):
        return
    hass.services.async_remove(DOMAIN, SERVICE_SET_ZONE_PARAMETER)
    hass.services.async_remove(DOMAIN, SERVICE_CLEANUP_AUTOMATION_GHOSTS)
