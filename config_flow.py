"""Config flow for ET₀ Irrigation — multi-step setup."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_ALTITUDE,
    CONF_BLOCKS,
    CONF_BLOCK_ZONES,
    CONF_IRRIGATION_TIME,
    CONF_MIN_DEFICIT,
    CONF_PRESSURE_UNIT,
    CONF_SENSOR_HUMIDITY,
    CONF_SENSOR_LUMINOSITY,
    CONF_SENSOR_PRESSURE,
    CONF_SENSOR_RAIN_TODAY,
    CONF_SENSOR_TEMPERATURE,
    CONF_TEMPERATURE_UNIT,
    CONF_SENSOR_WIND_SPEED,
    CONF_WIND_SPEED_UNIT,
    CONF_ZONE_FACTOR,
    CONF_ZONE_MAX_MINUTES,
    CONF_ZONE_MIN_MINUTES,
    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION,
    CONF_ZONE_COMPANION_POOL,
    CONF_ZONE_APPLICATION_RATE,
    CONF_ZONE_CREATED_AT,
    CONF_ZONE_NAME,
    CONF_ZONE_REQUIRES_COMPANION,
    CONF_ZONE_SWITCH,
    CONF_ZONE_TYPE,
    CONF_ZONES,
    DEFAULT_APPLICATION_RATE,
    DOMAIN,
)

ZONE_TYPE_ET0 = "et0"


def _validate_companion_constraints(zones: list[dict], blocks: list[dict]) -> str | None:
    """Validate companion restrictions shared by config/options flows."""
    zone_map = {z[CONF_ZONE_NAME]: z for z in zones}

    block_by_zone: dict[str, int] = {}
    for block_idx, block in enumerate(blocks):
        for zone_name in block.get(CONF_BLOCK_ZONES, []):
            block_by_zone[zone_name] = block_idx

    for zone in zones:
        zone_name = zone[CONF_ZONE_NAME]
        if not zone.get(CONF_ZONE_REQUIRES_COMPANION, False):
            continue

        companion_pool = zone.get(CONF_ZONE_COMPANION_POOL, []) or []
        if not companion_pool:
            return "invalid_companion_pool"

        companion_name = companion_pool[0]
        companion_zone = zone_map.get(companion_name)
        if companion_zone is None:
            return "invalid_companion_pool"

        if companion_zone.get(CONF_ZONE_REQUIRES_COMPANION, False):
            return "companion_chain_not_allowed"

        dependent_block = block_by_zone.get(zone_name)
        companion_block = block_by_zone.get(companion_name)
        if dependent_block is None or companion_block is None:
            return "companion_must_be_in_same_block"
        if dependent_block != companion_block:
            return "companion_must_be_in_same_block"

    return None


class ET0IrrigationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step config flow for ET₀ Irrigation."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}
        self._zones: list[dict] = []
        self._blocks: list[dict] = []
        self._current_zone: dict = {}
        self._current_block: dict = {}

    @staticmethod
    def async_get_options_flow(config_entry):
        return ET0IrrigationOptionsFlow(config_entry)

    # ------------------------------------------------------------------
    # Step 1 — Weather sensors
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            # Validate entities exist
            for key in [
                CONF_SENSOR_TEMPERATURE,
                CONF_SENSOR_HUMIDITY,
                CONF_SENSOR_WIND_SPEED,
                CONF_SENSOR_LUMINOSITY,
                CONF_SENSOR_PRESSURE,
                CONF_SENSOR_RAIN_TODAY,
            ]:
                if self.hass.states.get(user_input[key]) is None:
                    errors[key] = "entity_not_found"

            if not errors:
                self._data.update(user_input)
                return await self.async_step_general()

        schema = vol.Schema(
            {
                vol.Required(CONF_SENSOR_TEMPERATURE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_TEMPERATURE_UNIT, default="°C"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["°C", "°F"], mode="dropdown"
                    )
                ),
                vol.Required(CONF_SENSOR_HUMIDITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_SENSOR_WIND_SPEED): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_WIND_SPEED_UNIT, default="km/h"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["km/h", "m/s"], mode="dropdown"
                    )
                ),
                vol.Required(CONF_SENSOR_LUMINOSITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_SENSOR_PRESSURE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_PRESSURE_UNIT, default="hPa"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["hPa", "kPa"], mode="dropdown"
                    )
                ),
                vol.Required(CONF_SENSOR_RAIN_TODAY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 — General irrigation parameters
    # ------------------------------------------------------------------

    async def async_step_general(self, user_input=None):
        errors = {}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_zone_add()

        default_altitude = float(self.hass.config.elevation or 0.0)

        schema = vol.Schema(
            {
                vol.Required(CONF_IRRIGATION_TIME, default="02:00"): selector.TimeSelector(),
                vol.Required(CONF_MIN_DEFICIT, default=2.0): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=20.0, step=0.5, unit_of_measurement="mm", mode="slider"
                    )
                ),
                vol.Required(CONF_ALTITUDE, default=default_altitude): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=-500.0,
                        max=9000.0,
                        step=0.1,
                        unit_of_measurement="m",
                        mode="box",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="general",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3a — Add a zone (repeated until user is done)
    # ------------------------------------------------------------------

    async def async_step_zone_add(self, user_input=None):
        """Collect switch and type for a new zone."""
        errors = {}

        def _normalize_companion_pool(raw_value: Any) -> list[str]:
            if raw_value is None:
                return []
            if isinstance(raw_value, str):
                return [raw_value] if raw_value.strip() else []
            if isinstance(raw_value, list):
                return [item for item in raw_value if isinstance(item, str) and item.strip()]
            return []

        if user_input is not None:
            zone_switch = user_input[CONF_ZONE_SWITCH]
            requires_companion = bool(user_input.get(CONF_ZONE_REQUIRES_COMPANION, False))
            companion_pool = _normalize_companion_pool(user_input.get(CONF_ZONE_COMPANION_POOL))

            if self.hass.states.get(zone_switch) is None:
                errors[CONF_ZONE_SWITCH] = "entity_not_found"

            # Switches must be unique
            existing_switches = [z[CONF_ZONE_SWITCH] for z in self._zones]
            if zone_switch in existing_switches:
                errors[CONF_ZONE_SWITCH] = "duplicate_zone_switch"

            if zone_switch in companion_pool:
                errors[CONF_ZONE_COMPANION_POOL] = "invalid_companion_pool"

            if len(companion_pool) > 1:
                errors[CONF_ZONE_COMPANION_POOL] = "single_companion_only"

            if requires_companion and len(companion_pool) < 1:
                errors[CONF_ZONE_COMPANION_POOL] = "invalid_companion_pool"

            if not errors:
                switch_state = self.hass.states.get(zone_switch)
                switch_friendly_name = (
                    switch_state.attributes.get("friendly_name", zone_switch)
                    if switch_state is not None
                    else zone_switch
                )
                self._current_zone = {
                    # Keep "name" for backward compatibility with stored schema.
                    CONF_ZONE_NAME: zone_switch,
                    CONF_ZONE_CREATED_AT: datetime.utcnow().isoformat(),
                    CONF_ZONE_SWITCH: zone_switch,
                    "switch_friendly_name": switch_friendly_name,
                    CONF_ZONE_TYPE: ZONE_TYPE_ET0,
                    CONF_ZONE_APPLICATION_RATE: float(
                        user_input.get(CONF_ZONE_APPLICATION_RATE, DEFAULT_APPLICATION_RATE)
                    ),
                    CONF_ZONE_MAX_MINUTES: 30,
                    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION: int(
                        user_input.get(CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION, 0)
                    ),
                    CONF_ZONE_REQUIRES_COMPANION: requires_companion,
                    CONF_ZONE_COMPANION_POOL: companion_pool,
                }
                return await self.async_step_zone_et0()

        existing_switches = [z[CONF_ZONE_SWITCH] for z in self._zones]
        companion_options = [
            {"value": switch, "label": self._zone_friendly_name(switch)}
            for switch in existing_switches
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE_SWITCH): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Required(
                    CONF_ZONE_APPLICATION_RATE,
                    default=DEFAULT_APPLICATION_RATE,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1,
                        max=2.0,
                        step=0.001,
                        mode="box",
                        unit_of_measurement="mm/min",
                    )
                ),
                vol.Required(
                    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION,
                    default=0,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=5,
                        step=1,
                        mode="slider",
                    )
                ),
                vol.Required(CONF_ZONE_REQUIRES_COMPANION, default=False): selector.BooleanSelector(),
                vol.Optional(CONF_ZONE_COMPANION_POOL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=companion_options,
                        multiple=False,
                        mode="dropdown",
                    )
                ),
            }
        )

        # Show how many zones have been added so far
        description_placeholders = {
            "zones_added": str(len(self._zones)),
            "zone_list": self._zone_list_text() or "—",
        }

        return self.async_show_form(
            step_id="zone_add",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    # ------------------------------------------------------------------
    # Step 3b — ET₀ zone parameters (exposure factor)
    # ------------------------------------------------------------------

    async def async_step_zone_et0(self, user_input=None):
        if user_input is not None:
            self._current_zone[CONF_ZONE_FACTOR] = float(user_input[CONF_ZONE_FACTOR])
            self._current_zone[CONF_ZONE_MIN_MINUTES] = int(user_input.get(CONF_ZONE_MIN_MINUTES) or 0)
            self._current_zone[CONF_ZONE_MAX_MINUTES] = int(user_input.get(CONF_ZONE_MAX_MINUTES) or 30)
            self._zones.append(self._current_zone)
            self._current_zone = {}
            return await self.async_step_zone_more()

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE_FACTOR, default=1.0): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1, max=2.0, step=0.05, mode="slider"
                    )
                ),
                vol.Optional(CONF_ZONE_MIN_MINUTES, default=0): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=120, step=1, unit_of_measurement="min", mode="box"
                    )
                ),
                vol.Required(CONF_ZONE_MAX_MINUTES, default=30): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=240, step=1, unit_of_measurement="min", mode="box"
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="zone_et0",
            data_schema=schema,
            description_placeholders={
                "zone_name": self._zone_friendly_name(self._current_zone[CONF_ZONE_SWITCH])
            },
        )

    # ------------------------------------------------------------------
    # Step 3d — Add another zone or proceed to blocks?
    # ------------------------------------------------------------------

    async def async_step_zone_more(self, user_input=None):
        if user_input is not None:
            if user_input.get("add_more"):
                return await self.async_step_zone_add()
            else:
                if len(self._zones) < 1:
                    return await self.async_step_zone_add()
                return await self.async_step_block_add()

        schema = vol.Schema(
            {
                vol.Required("add_more", default=True): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="zone_more",
            data_schema=schema,
            description_placeholders={
                "zones_added": str(len(self._zones)),
                "zone_list": self._zone_list_text(),
            },
        )

    # ------------------------------------------------------------------
    # Step 4a — Add a block
    # ------------------------------------------------------------------

    async def async_step_block_add(self, user_input=None):
        """Select which zones participate in this block."""
        errors = {}
        zone_options = [
            {
                "value": z[CONF_ZONE_NAME],
                "label": self._zone_friendly_name(z[CONF_ZONE_SWITCH]),
            }
            for z in self._zones
        ]

        if user_input is not None:
            selected = user_input[CONF_BLOCK_ZONES]

            if len(selected) < 1:
                errors[CONF_BLOCK_ZONES] = "block_needs_zone"

            if not errors:
                self._blocks.append(
                    {
                        CONF_BLOCK_ZONES: selected,
                    }
                )
                return await self.async_step_block_more()

        schema = vol.Schema(
            {
                vol.Required(CONF_BLOCK_ZONES): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=zone_options,
                        multiple=True,
                        mode="list",
                    )
                ),
            }
        )

        description_placeholders = {
            "blocks_added": str(len(self._blocks)),
            "block_list": (
                " → ".join(
                    self._format_block_label(i + 1, b)
                    for i, b in enumerate(self._blocks)
                )
                or "—"
            ),
        }

        return self.async_show_form(
            step_id="block_add",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    # ------------------------------------------------------------------
    # Step 4b — Add another block or finish?
    # ------------------------------------------------------------------

    async def async_step_block_more(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("add_more"):
                return await self.async_step_block_add()
            else:
                validation_error = _validate_companion_constraints(self._zones, self._blocks)
                if validation_error:
                    errors["base"] = validation_error
                else:
                    return self._create_entry()

        schema = vol.Schema(
            {
                vol.Required("add_more", default=False): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="block_more",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "blocks_added": str(len(self._blocks)),
                "block_list": " → ".join(
                    self._format_block_label(i + 1, b)
                    for i, b in enumerate(self._blocks)
                ),
            },
        )

    def _zone_friendly_name(self, zone_switch: str) -> str:
        for zone in self._zones:
            if zone.get(CONF_ZONE_SWITCH) == zone_switch:
                return zone.get("switch_friendly_name", zone_switch)

        switch_state = self.hass.states.get(zone_switch)
        if switch_state is not None:
            return switch_state.attributes.get("friendly_name", zone_switch)
        return zone_switch

    def _zone_list_text(self) -> str:
        return ", ".join(self._zone_friendly_name(z[CONF_ZONE_SWITCH]) for z in self._zones)

    def _format_block_label(self, idx: int, block: dict) -> str:
        zone_labels = [self._zone_friendly_name(z) for z in block.get(CONF_BLOCK_ZONES, [])]
        return f"Grupo {idx}: [{', '.join(zone_labels)}]"

    # ------------------------------------------------------------------
    # Finalise
    # ------------------------------------------------------------------

    def _create_entry(self):
        # Fixed behavior: always use a 5-day deficit/history window.
        self._data["history_days"] = 5
        self._data["deficit_sensor_days"] = 5
        self._data[CONF_ZONES] = self._zones
        self._data[CONF_BLOCKS] = self._blocks
        return self.async_create_entry(title="ET₀ Irrigation", data=self._data)


class ET0IrrigationOptionsFlow(config_entries.OptionsFlow):
    """Options flow to edit configurable variables after setup."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry
        self._working_data: dict = {}
        self._zones: list[dict] = []
        self._blocks: list[dict] = []
        self._current_zone: dict = {}

    def _validate_zones_and_blocks(self, zones: Any, blocks: Any) -> str | None:
        """Validate zones/blocks object payload from options form."""
        if not isinstance(zones, list) or not zones:
            return "invalid_zones"

        zone_names: list[str] = []
        zone_switches: list[str] = []
        for zone in zones:
            if not isinstance(zone, dict):
                return "invalid_zones"

            switch = zone.get(CONF_ZONE_SWITCH)
            ztype = zone.get(CONF_ZONE_TYPE)

            if not isinstance(switch, str) or self.hass.states.get(switch) is None:
                return "entity_not_found"

            if switch in zone_switches:
                return "duplicate_zone_switch"
            zone_switches.append(switch)

            # Keep "name" for backward compatibility with stored schema.
            name = zone.get(CONF_ZONE_NAME) or switch
            if not isinstance(name, str) or not name.strip():
                return "invalid_zones"
            if name in zone_names:
                return "duplicate_zone_name"
            zone_names.append(name)
            zone[CONF_ZONE_NAME] = name
            if not zone.get("switch_friendly_name"):
                switch_state = self.hass.states.get(switch)
                zone["switch_friendly_name"] = (
                    switch_state.attributes.get("friendly_name", switch)
                    if switch_state is not None
                    else switch
                )

            try:
                factor = float(zone.get(CONF_ZONE_FACTOR, 1.0))
            except (TypeError, ValueError):
                return "invalid_zones"
            if factor < 0.1 or factor > 2.0:
                return "invalid_zones"
            zone[CONF_ZONE_FACTOR] = factor

            # All zones are ET0-based; normalise min_minutes (0 = no minimum)
            try:
                min_minutes = max(0, int(zone.get(CONF_ZONE_MIN_MINUTES, 0) or 0))
            except (TypeError, ValueError):
                min_minutes = 0
            zone[CONF_ZONE_MIN_MINUTES] = min_minutes

            try:
                max_minutes = int(zone.get(CONF_ZONE_MAX_MINUTES, 30) or 30)
            except (TypeError, ValueError):
                return "invalid_zones"
            if max_minutes < 1 or max_minutes > 240:
                return "invalid_zones"
            if max_minutes < min_minutes:
                max_minutes = min_minutes
            zone[CONF_ZONE_MAX_MINUTES] = max_minutes

            if ztype != ZONE_TYPE_ET0:
                return "invalid_zones"
            zone[CONF_ZONE_TYPE] = ZONE_TYPE_ET0

            requires_companion = bool(zone.get(CONF_ZONE_REQUIRES_COMPANION, False))
            companion_pool = zone.get(CONF_ZONE_COMPANION_POOL, [])
            application_rate = zone.get(CONF_ZONE_APPLICATION_RATE, DEFAULT_APPLICATION_RATE)
            max_days = zone.get(CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION, 0)

            try:
                max_days_int = int(max_days)
            except (TypeError, ValueError):
                return "invalid_zones"
            if max_days_int < 0 or max_days_int > 5:
                return "invalid_zones"
            zone[CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION] = max_days_int

            try:
                application_rate_float = float(application_rate)
            except (TypeError, ValueError):
                return "invalid_zones"
            if application_rate_float <= 0:
                return "invalid_zones"
            zone[CONF_ZONE_APPLICATION_RATE] = application_rate_float

            if companion_pool is None:
                companion_pool = []
            if not isinstance(companion_pool, list):
                return "invalid_zones"
            if any(not isinstance(item, str) or not item.strip() for item in companion_pool):
                return "invalid_zones"
            if len(companion_pool) > 1:
                return "single_companion_only"
            if requires_companion and len(companion_pool) < 1:
                return "invalid_companion_pool"

            # Keep normalized keys in saved options payload.
            zone[CONF_ZONE_REQUIRES_COMPANION] = requires_companion
            zone[CONF_ZONE_COMPANION_POOL] = companion_pool

        if not isinstance(blocks, list) or not blocks:
            return "invalid_blocks"

        for block in blocks:
            if not isinstance(block, dict):
                return "invalid_blocks"

            block_zones = block.get(CONF_BLOCK_ZONES)

            if not isinstance(block_zones, list) or not block_zones:
                return "block_needs_zone"
            if any(z not in zone_names for z in block_zones):
                return "invalid_blocks"

        # Companion pools must reference existing zones and not self.
        for zone in zones:
            zname = zone[CONF_ZONE_NAME]
            companion_pool = zone.get(CONF_ZONE_COMPANION_POOL, [])
            if any(c not in zone_names for c in companion_pool):
                return "invalid_companion_pool"
            if zname in companion_pool:
                return "invalid_companion_pool"

        companion_error = _validate_companion_constraints(zones, blocks)
        if companion_error:
            return companion_error

        return None

    async def async_step_init(self, user_input=None):
        errors = {}

        current = {**self._config_entry.data, **self._config_entry.options}
        self._zones = list(current.get(CONF_ZONES, []))
        self._blocks = list(current.get(CONF_BLOCKS, []))

        if user_input is not None:
            for key in [
                CONF_SENSOR_TEMPERATURE,
                CONF_SENSOR_HUMIDITY,
                CONF_SENSOR_WIND_SPEED,
                CONF_SENSOR_LUMINOSITY,
                CONF_SENSOR_PRESSURE,
                CONF_SENSOR_RAIN_TODAY,
            ]:
                if self.hass.states.get(user_input[key]) is None:
                    errors[key] = "entity_not_found"

            if not errors:
                default_altitude = float(self.hass.config.elevation or 0.0)
                self._working_data = {
                    CONF_SENSOR_TEMPERATURE: user_input[CONF_SENSOR_TEMPERATURE],
                    CONF_TEMPERATURE_UNIT: user_input[CONF_TEMPERATURE_UNIT],
                    CONF_SENSOR_HUMIDITY: user_input[CONF_SENSOR_HUMIDITY],
                    CONF_SENSOR_WIND_SPEED: user_input[CONF_SENSOR_WIND_SPEED],
                    CONF_SENSOR_LUMINOSITY: user_input[CONF_SENSOR_LUMINOSITY],
                    CONF_SENSOR_PRESSURE: user_input[CONF_SENSOR_PRESSURE],
                    CONF_SENSOR_RAIN_TODAY: user_input[CONF_SENSOR_RAIN_TODAY],
                    CONF_WIND_SPEED_UNIT: user_input[CONF_WIND_SPEED_UNIT],
                    CONF_PRESSURE_UNIT: user_input[CONF_PRESSURE_UNIT],
                    CONF_IRRIGATION_TIME: user_input[CONF_IRRIGATION_TIME],
                    CONF_MIN_DEFICIT: user_input[CONF_MIN_DEFICIT],
                    CONF_ALTITUDE: user_input.get(CONF_ALTITUDE, default_altitude),
                }
                if user_input.get("reconfigure_layout"):
                    return await self.async_step_zone_more()
                return self._create_options_entry(self._zones, self._blocks)

        default_altitude = float(current.get(CONF_ALTITUDE, self.hass.config.elevation or 0.0) or 0.0)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SENSOR_TEMPERATURE,
                    default=current.get(CONF_SENSOR_TEMPERATURE),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    CONF_TEMPERATURE_UNIT,
                    default=current.get(CONF_TEMPERATURE_UNIT, "°C"),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["°C", "°F"], mode="dropdown")
                ),
                vol.Required(
                    CONF_SENSOR_HUMIDITY,
                    default=current.get(CONF_SENSOR_HUMIDITY),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    CONF_SENSOR_WIND_SPEED,
                    default=current.get(CONF_SENSOR_WIND_SPEED),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    CONF_WIND_SPEED_UNIT,
                    default=current.get(CONF_WIND_SPEED_UNIT, "km/h"),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["km/h", "m/s"], mode="dropdown")
                ),
                vol.Required(
                    CONF_SENSOR_LUMINOSITY,
                    default=current.get(CONF_SENSOR_LUMINOSITY),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    CONF_SENSOR_PRESSURE,
                    default=current.get(CONF_SENSOR_PRESSURE),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    CONF_PRESSURE_UNIT,
                    default=current.get(CONF_PRESSURE_UNIT, "hPa"),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["hPa", "kPa"], mode="dropdown")
                ),
                vol.Required(
                    CONF_SENSOR_RAIN_TODAY,
                    default=current.get(CONF_SENSOR_RAIN_TODAY),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    CONF_IRRIGATION_TIME,
                    default=current.get(CONF_IRRIGATION_TIME, "02:00"),
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_MIN_DEFICIT,
                    default=current.get(CONF_MIN_DEFICIT, 2.0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=20.0, step=0.5, unit_of_measurement="mm", mode="slider"
                    )
                ),
                vol.Required(CONF_ALTITUDE, default=default_altitude): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=-500.0,
                        max=9000.0,
                        step=0.1,
                        unit_of_measurement="m",
                        mode="box",
                    )
                ),
                vol.Required("reconfigure_layout", default=False): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def async_step_zone_add(self, user_input=None):
        errors = {}

        def _normalize_companion(raw_value: Any) -> list[str]:
            if raw_value is None:
                return []
            if isinstance(raw_value, str):
                return [raw_value] if raw_value.strip() else []
            if isinstance(raw_value, list):
                return [item for item in raw_value if isinstance(item, str) and item.strip()]
            return []

        if user_input is not None:
            zone_switch = user_input[CONF_ZONE_SWITCH]
            requires_companion = bool(user_input.get(CONF_ZONE_REQUIRES_COMPANION, False))
            companion_pool = _normalize_companion(user_input.get(CONF_ZONE_COMPANION_POOL))

            if self.hass.states.get(zone_switch) is None:
                errors[CONF_ZONE_SWITCH] = "entity_not_found"

            existing_switches = [z[CONF_ZONE_SWITCH] for z in self._zones]
            if zone_switch in existing_switches:
                errors[CONF_ZONE_SWITCH] = "duplicate_zone_switch"
            if zone_switch in companion_pool:
                errors[CONF_ZONE_COMPANION_POOL] = "invalid_companion_pool"
            if len(companion_pool) > 1:
                errors[CONF_ZONE_COMPANION_POOL] = "single_companion_only"
            if requires_companion and len(companion_pool) < 1:
                errors[CONF_ZONE_COMPANION_POOL] = "invalid_companion_pool"

            if not errors:
                switch_state = self.hass.states.get(zone_switch)
                switch_friendly_name = (
                    switch_state.attributes.get("friendly_name", zone_switch)
                    if switch_state is not None
                    else zone_switch
                )
                self._current_zone = {
                    CONF_ZONE_NAME: zone_switch,
                    CONF_ZONE_CREATED_AT: datetime.utcnow().isoformat(),
                    CONF_ZONE_SWITCH: zone_switch,
                    "switch_friendly_name": switch_friendly_name,
                    CONF_ZONE_TYPE: ZONE_TYPE_ET0,
                    CONF_ZONE_APPLICATION_RATE: float(
                        user_input.get(CONF_ZONE_APPLICATION_RATE, DEFAULT_APPLICATION_RATE)
                    ),
                    CONF_ZONE_MAX_MINUTES: 30,
                    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION: int(
                        user_input.get(CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION, 0)
                    ),
                    CONF_ZONE_REQUIRES_COMPANION: requires_companion,
                    CONF_ZONE_COMPANION_POOL: companion_pool,
                }
                return await self.async_step_zone_et0()

        existing_switches = [z[CONF_ZONE_SWITCH] for z in self._zones]
        companion_options = [
            {"value": switch, "label": self._zone_friendly_name(switch)}
            for switch in existing_switches
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE_SWITCH): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Required(
                    CONF_ZONE_APPLICATION_RATE,
                    default=DEFAULT_APPLICATION_RATE,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1,
                        max=2.0,
                        step=0.001,
                        mode="box",
                        unit_of_measurement="mm/min",
                    )
                ),
                vol.Required(
                    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION,
                    default=0,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=5,
                        step=1,
                        mode="slider",
                    )
                ),
                vol.Required(CONF_ZONE_REQUIRES_COMPANION, default=False): selector.BooleanSelector(),
                vol.Optional(CONF_ZONE_COMPANION_POOL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=companion_options,
                        multiple=False,
                        mode="dropdown",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="zone_add",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "zones_added": str(len(self._zones)),
                "zone_list": self._zone_list_text() or "—",
            },
        )

    async def async_step_zone_et0(self, user_input=None):
        if user_input is not None:
            self._current_zone[CONF_ZONE_FACTOR] = float(user_input[CONF_ZONE_FACTOR])
            self._current_zone[CONF_ZONE_MIN_MINUTES] = int(user_input.get(CONF_ZONE_MIN_MINUTES) or 0)
            self._current_zone[CONF_ZONE_MAX_MINUTES] = int(user_input.get(CONF_ZONE_MAX_MINUTES) or 30)
            self._zones.append(self._current_zone)
            self._current_zone = {}
            return await self.async_step_zone_more()

        existing_min = int(self._current_zone.get(CONF_ZONE_MIN_MINUTES) or 0)
        existing_max = int(self._current_zone.get(CONF_ZONE_MAX_MINUTES) or 30)
        existing_factor = float(self._current_zone.get(CONF_ZONE_FACTOR, 1.0))
        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE_FACTOR, default=existing_factor): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.1, max=2.0, step=0.05, mode="slider")
                ),
                vol.Optional(CONF_ZONE_MIN_MINUTES, default=existing_min): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=120, step=1, unit_of_measurement="min", mode="box")
                ),
                vol.Required(CONF_ZONE_MAX_MINUTES, default=existing_max): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=240, step=1, unit_of_measurement="min", mode="box")
                ),
            }
        )
        return self.async_show_form(step_id="zone_et0", data_schema=schema)

    async def async_step_zone_more(self, user_input=None):
        errors = {}
        existing_zone_options = [
            {
                "value": z[CONF_ZONE_SWITCH],
                "label": self._zone_friendly_name(z[CONF_ZONE_SWITCH]),
            }
            for z in self._zones
        ]

        existing_zone_options_inner = [
            {
                "value": z[CONF_ZONE_SWITCH],
                "label": self._zone_friendly_name(z[CONF_ZONE_SWITCH]),
            }
            for z in self._zones
        ]
        schema_fields_inner = {}
        if existing_zone_options_inner:
            schema_fields_inner[vol.Optional("remove_zone_switch")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=existing_zone_options_inner,
                    multiple=True,
                    mode="list",
                )
            )
        schema_fields_inner[vol.Required("add_more", default=True)] = selector.BooleanSelector()
        if existing_zone_options_inner:
            schema_fields_inner[vol.Optional("confirm_zone_deletion", default=False)] = selector.BooleanSelector()
        inner_schema = vol.Schema(schema_fields_inner)

        if user_input is not None:
            zones_to_remove = self._normalize_string_list(user_input.get("remove_zone_switch"))
            if zones_to_remove:
                if user_input.get("confirm_zone_deletion"):
                    self._remove_zones(zones_to_remove)
                    if len(self._zones) < 1:
                        return await self.async_step_zone_add()
                    return await self.async_step_zone_more()
                errors["confirm_zone_deletion"] = "confirm_delete_required"
                return self.async_show_form(
                    step_id="zone_more",
                    data_schema=inner_schema,
                    errors=errors,
                    description_placeholders={"zones_added": str(len(self._zones))},
                )

            if user_input.get("add_more"):
                return await self.async_step_zone_add()
            if len(self._zones) < 1:
                return await self.async_step_zone_add()
            if len(self._blocks) >= 1:
                return await self.async_step_block_more()
            return await self.async_step_block_add()

        schema_fields = {}
        if existing_zone_options:
            schema_fields[vol.Optional("remove_zone_switch")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=existing_zone_options,
                    multiple=True,
                    mode="list",
                )
            )
        schema_fields[vol.Required("add_more", default=True)] = selector.BooleanSelector()
        if existing_zone_options:
            schema_fields[vol.Optional("confirm_zone_deletion", default=False)] = selector.BooleanSelector()

        schema = vol.Schema(schema_fields)
        return self.async_show_form(
            step_id="zone_more",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "zones_added": str(len(self._zones)),
            },
        )

    async def async_step_block_add(self, user_input=None):
        errors = {}
        zone_options = [
            {
                "value": z[CONF_ZONE_NAME],
                "label": self._zone_friendly_name(z[CONF_ZONE_SWITCH]),
            }
            for z in self._zones
        ]
        if user_input is not None:
            selected = user_input[CONF_BLOCK_ZONES]
            if len(selected) < 1:
                errors[CONF_BLOCK_ZONES] = "block_needs_zone"
            if not errors:
                self._blocks.append({CONF_BLOCK_ZONES: selected})
                return await self.async_step_block_more()

        schema = vol.Schema(
            {
                vol.Required(CONF_BLOCK_ZONES): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=zone_options, multiple=True, mode="list")
                )
            }
        )
        return self.async_show_form(
            step_id="block_add",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "blocks_added": str(len(self._blocks)),
                "block_list": " → ".join(
                    self._format_block_label(i + 1, b)
                    for i, b in enumerate(self._blocks)
                )
                or "—",
            },
        )

    async def async_step_block_more(self, user_input=None):
        errors = {}
        block_options = [
            {
                "value": str(i),
                "label": self._format_block_label(i + 1, b),
            }
            for i, b in enumerate(self._blocks)
        ]

        if user_input is not None:
            block_to_remove = self._normalize_string_list(user_input.get("remove_block"))
            if block_to_remove:
                if user_input.get("confirm_block_deletion"):
                    idx_to_remove = {
                        int(value)
                        for value in block_to_remove
                        if value.isdigit() and 0 <= int(value) < len(self._blocks)
                    }
                    if idx_to_remove:
                        self._blocks = [
                            block for i, block in enumerate(self._blocks) if i not in idx_to_remove
                        ]
                    if len(self._blocks) < 1:
                        return await self.async_step_block_add()
                    return await self.async_step_block_more()
                errors["confirm_block_deletion"] = "confirm_delete_required"
                # Rebuild schema to re-show form with error
                block_options_inner = [
                    {"value": str(i), "label": self._format_block_label(i + 1, b)}
                    for i, b in enumerate(self._blocks)
                ]
                sf = {}
                if block_options_inner:
                    sf[vol.Optional("remove_block")] = selector.SelectSelector(
                        selector.SelectSelectorConfig(options=block_options_inner, multiple=True, mode="list")
                    )
                sf[vol.Required("add_more", default=False)] = selector.BooleanSelector()
                if block_options_inner:
                    sf[vol.Optional("confirm_block_deletion", default=False)] = selector.BooleanSelector()
                return self.async_show_form(
                    step_id="block_more",
                    data_schema=vol.Schema(sf),
                    errors=errors,
                    description_placeholders={"blocks_added": str(len(self._blocks))},
                )

            if user_input.get("add_more"):
                return await self.async_step_block_add()
            if len(self._blocks) < 1:
                return await self.async_step_block_add()
            return self._create_options_entry(self._zones, self._blocks)

        schema_fields = {}
        if block_options:
            schema_fields[vol.Optional("remove_block")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=block_options,
                    multiple=True,
                    mode="list",
                )
            )
        schema_fields[vol.Required("add_more", default=False)] = selector.BooleanSelector()
        if block_options:
            schema_fields[vol.Optional("confirm_block_deletion", default=False)] = selector.BooleanSelector()

        schema = vol.Schema(schema_fields)
        return self.async_show_form(
            step_id="block_more",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "blocks_added": str(len(self._blocks)),
            },
        )

    def _zone_friendly_name(self, zone_switch: str) -> str:
        for zone in self._zones:
            if zone.get(CONF_ZONE_SWITCH) == zone_switch:
                return zone.get("switch_friendly_name", zone_switch)

        switch_state = self.hass.states.get(zone_switch)
        if switch_state is not None:
            return switch_state.attributes.get("friendly_name", zone_switch)
        return zone_switch

    def _zone_list_text(self) -> str:
        return ", ".join(self._zone_friendly_name(z[CONF_ZONE_SWITCH]) for z in self._zones)

    def _format_block_label(self, idx: int, block: dict) -> str:
        zone_labels = [self._zone_friendly_name(z) for z in block.get(CONF_BLOCK_ZONES, [])]
        return f"Grupo {idx}: [{', '.join(zone_labels)}]"

    def _normalize_string_list(self, raw_value) -> list[str]:
        if isinstance(raw_value, str):
            return [raw_value] if raw_value.strip() else []
        if isinstance(raw_value, list):
            return [value for value in raw_value if isinstance(value, str) and value.strip()]
        return []

    def _remove_zones(self, zone_switches: list[str]) -> None:
        to_remove = set(zone_switches)
        self._zones = [z for z in self._zones if z.get(CONF_ZONE_SWITCH) not in to_remove]

        for zone in self._zones:
            companion_pool = [
                value
                for value in zone.get(CONF_ZONE_COMPANION_POOL, [])
                if value not in to_remove
            ]
            zone[CONF_ZONE_COMPANION_POOL] = companion_pool
            if zone.get(CONF_ZONE_REQUIRES_COMPANION) and len(companion_pool) < 1:
                zone[CONF_ZONE_REQUIRES_COMPANION] = False

        filtered_blocks = []
        for block in self._blocks:
            remaining = [
                value for value in block.get(CONF_BLOCK_ZONES, []) if value not in to_remove
            ]
            if remaining:
                filtered_blocks.append({CONF_BLOCK_ZONES: remaining})
        self._blocks = filtered_blocks

    def _create_options_entry(self, zones: list[dict], blocks: list[dict]):
        validation_error = self._validate_zones_and_blocks(zones, blocks)
        if validation_error:
            return self.async_abort(reason=validation_error)

        data = {
            **self._working_data,
            CONF_ZONES: zones,
            CONF_BLOCKS: blocks,
            "history_days": 5,
            "deficit_sensor_days": 5,
        }
        return self.async_create_entry(title="", data=data)
