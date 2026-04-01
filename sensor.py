"""ET₀ Irrigation sensors — Penman-Monteith FAO-56."""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import DOMAIN
from .const import (
    CONF_ALTITUDE,
    CONF_SENSOR_RAIN_TODAY,
    CONF_ZONE_APPLICATION_RATE,
    CONF_ZONE_CREATED_AT,
    CONF_ZONE_FACTOR,
    CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION,
    CONF_ZONE_SWITCH,
    CONF_ZONE_TYPE,
    CONF_ZONES,
    DEFAULT_APPLICATION_RATE,
    DEFAULT_MIN_SURPLUS_FLOOR_MM_PER_DAY,
)

_LOGGER = logging.getLogger(__name__)

# How often sensors update (every 30 minutes is sufficient)
SCAN_INTERVAL = timedelta(minutes=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ET₀ sensors from config entry."""
    config = hass.data[DOMAIN][entry.entry_id]
    zones = config.get(CONF_ZONES, [])

    # Initialize rolling history of completed-day ET0 values (last 7 days)
    # for dynamic surplus floor calculation.  Values are pushed once per day
    # at midnight rollover — NOT at every 30-minute poll cycle.
    config["et0_daily_history"] = deque(maxlen=7)

    # Build entities first so we can store a direct object reference to the
    # WaterDeficitSensor(1d).  ZoneWaterDeficitSensor reads it via this
    # reference instead of a hardcoded entity_id string, which is fragile
    # when the HA entity registry assigns a suffix due to name conflicts.
    water_deficit_1d = WaterDeficitSensor(hass, entry, config, 1)
    config["water_deficit_1d_entity"] = water_deficit_1d

    zone_deficit_entities = [
        ZoneWaterDeficitSensor(hass, entry, config, zone)
        for zone in zones
        if isinstance(zone, dict) and zone.get(CONF_ZONE_SWITCH)
    ]

    et0_today = ET0TodaySensor(hass, entry, config)
    config["et0_today_entity"] = et0_today

    entities = [
        et0_today,
        water_deficit_1d,
        *zone_deficit_entities,
    ]
    config["zone_deficit_entities"] = zone_deficit_entities
    async_add_entities(entities, update_before_add=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(state_obj) -> float | None:
    """Extract a float from a HA state object, returning None if unavailable."""
    if state_obj is None:
        return None
    try:
        val = float(state_obj.state)
        return val if math.isfinite(val) else None
    except (ValueError, TypeError):
        return None


async def _daily_stats(
    hass: HomeAssistant,
    entity_id: str,
    date: datetime,
) -> dict[str, float | None]:
    """
    Return mean, min, max, and sum for *entity_id* over the calendar day
    defined by *date* (in local time).

    Uses recorder get_significant_states so it works without statistics
    tables (which require long-term stats to be enabled for the sensor).
    """
    local_tz = dt_util.get_time_zone(hass.config.time_zone)
    day_start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=local_tz)
    day_end = day_start + timedelta(days=1)

    def _fetch():
        return get_significant_states(
            hass,
            day_start,
            day_end,
            [entity_id],
            significant_changes_only=False,
        )

    history = await get_instance(hass).async_add_executor_job(_fetch)
    states = history.get(entity_id, [])

    values = []
    for s in states:
        v = _safe_float(s)
        if v is not None:
            values.append(v)

    if not values:
        return {"mean": None, "min": None, "max": None, "sum": None, "count": 0}

    return {
        "mean": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
        "sum": sum(values),
        "count": len(values),
    }


async def _daily_irradiation_wh_m2(
    hass: HomeAssistant,
    entity_id: str,
    date: datetime,
) -> float:
    """
    Integrate lux readings over a calendar day using the trapezoidal rule.
    Returns Wh/m²  (lux / 120 = W/m², integrated over hours).
    """
    local_tz = dt_util.get_time_zone(hass.config.time_zone)
    day_start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=local_tz)
    day_end = day_start + timedelta(days=1)

    def _fetch():
        return get_significant_states(
            hass,
            day_start,
            day_end,
            [entity_id],
            significant_changes_only=False,
        )

    history = await get_instance(hass).async_add_executor_job(_fetch)
    states = history.get(entity_id, [])

    points: list[tuple[float, float]] = []  # (epoch_seconds, W/m²)
    for s in states:
        v = _safe_float(s)
        if v is not None:
            t = s.last_changed.timestamp()
            points.append((t, v / 120.0))

    if len(points) < 2:
        return 0.0

    # Trapezoidal integration → result in W·s/m², convert to Wh/m²
    energy = 0.0
    for i in range(1, len(points)):
        dt_s = points[i][0] - points[i - 1][0]
        avg_w = (points[i][1] + points[i - 1][1]) / 2.0
        energy += avg_w * dt_s

    return energy / 3600.0  # Wh/m²


def _temperature_to_celsius(value: float, unit: str) -> float:
    """Convert temperature to Celsius when needed."""
    if unit == "°F":
        return (value - 32.0) * (5.0 / 9.0)
    return value


def _zone_slug(zone_switch: str) -> str:
    """Create deterministic slug from a zone switch entity id."""
    ascii_name = (
        unicodedata.normalize("NFKD", str(zone_switch))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")


def _zone_deficit_entity_id(zone_switch: str) -> str:
    return f"sensor.et0_irrigation_zone_deficit_{_zone_slug(zone_switch)}"


def _extraterrestrial_radiation(latitude_deg: float, day_of_year: int) -> float:
    """
    Extraterrestrial radiation Ra (MJ/m²/day) — FAO-56 equations 21-25.

    Args:
        latitude_deg: Latitude in decimal degrees (positive = North, negative = South)
        day_of_year:  Day of the year (1–365/366)

    Returns:
        Ra in MJ/m²/day (≥ 0.1 to avoid division by zero at polar winter)
    """
    phi = math.radians(latitude_deg)

    # Inverse relative distance Earth–Sun (eq. 23)
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)

    # Solar declination (eq. 24)
    delta = 0.409 * math.sin(2.0 * math.pi * day_of_year / 365.0 - 1.39)

    # Sunset hour angle (eq. 25) — clamped to avoid domain errors at polar lat.
    arg = -math.tan(phi) * math.tan(delta)
    arg = max(-1.0, min(1.0, arg))
    omega_s = math.acos(arg)

    # Ra (eq. 21) — solar constant Gsc = 0.0820 MJ/(m²·min)
    gsc = 0.0820
    ra = (
        (24.0 * 60.0 / math.pi)
        * gsc
        * dr
        * (
            omega_s * math.sin(phi) * math.sin(delta)
            + math.cos(phi) * math.cos(delta) * math.sin(omega_s)
        )
    )
    return max(ra, 0.1)


def _penman_monteith(
    t_mean: float,
    rh_mean: float,
    u2_ms: float,
    rs_wh_m2: float,
    pressure_kpa: float,
    latitude_deg: float,
    day_of_year: int,
) -> float:
    """
    Penman-Monteith FAO-56 reference evapotranspiration (mm/day).

    Args:
        t_mean:        Mean daily air temperature (°C)
        rh_mean:       Mean daily relative humidity (%)
        u2_ms:         Mean daily wind speed at 2 m height (m/s)
        rs_wh_m2:      Daily solar irradiation (Wh/m²)
        pressure_kpa:  Atmospheric pressure (kPa)
        latitude_deg:  Latitude in decimal degrees (from hass.config.latitude)
        day_of_year:   Day of the year (1–365/366)

    Returns:
        ET₀ in mm/day (≥ 0)

    Simplifications vs full FAO-56:
        - Net longwave radiation uses T_mean instead of T_max/T_min
        - Soil heat flux G = 0 (negligible for daily timestep)
        - Ra is computed dynamically from latitude and day-of-year
          using FAO-56 equations 21-25 (no longer a fixed constant)
    """
    # Solar radiation MJ/m²
    rs_mj = rs_wh_m2 * 0.0036

    # Saturation vapour pressure (kPa)
    es = 0.6108 * math.exp(17.27 * t_mean / (t_mean + 237.3))
    # Actual vapour pressure
    ea = es * rh_mean / 100.0

    # Slope of saturation vapour pressure curve (kPa/°C)
    delta = 4098.0 * es / (t_mean + 237.3) ** 2

    # Psychrometric constant (kPa/°C)
    gamma = 0.000665 * pressure_kpa

    # Net shortwave radiation (αcanopy = 0.23)
    rns = (1.0 - 0.23) * rs_mj

    # Net longwave radiation — Ra computed dynamically per FAO-56 eqs 21-25
    sigma = 4.903e-9  # MJ/(m²·K⁴·day)
    tk = t_mean + 273.16
    ra_ref = _extraterrestrial_radiation(latitude_deg, day_of_year)
    rs_over_rso = min(rs_mj / (0.75 * ra_ref), 1.0)
    rnl = (
        sigma
        * tk ** 4
        * (0.34 - 0.14 * math.sqrt(max(ea, 0.0)))
        * (1.35 * rs_over_rso - 0.35)
    )

    rn = rns - rnl  # Net radiation MJ/m²/day

    # Penman-Monteith FAO-56
    numerator = 0.408 * delta * rn + gamma * (900.0 / (t_mean + 273.0)) * u2_ms * (es - ea)
    denominator = delta + gamma * (1.0 + 0.34 * u2_ms)

    et0 = numerator / denominator
    return max(et0, 0.0)


def _resolve_altitude_m(hass: HomeAssistant, config: dict) -> float:
    """Return altitude in meters using integration config with HA fallback."""
    altitude = config.get(CONF_ALTITUDE, hass.config.elevation)
    try:
        return float(0.0 if altitude is None else altitude)
    except (TypeError, ValueError):
        try:
            return float(hass.config.elevation or 0.0)
        except (TypeError, ValueError):
            return 0.0


def _pressure_from_altitude_kpa(altitude_m: float) -> float:
    """Estimate atmospheric pressure from altitude (FAO-56 approximation)."""
    return max(0.0, 101.3 * ((293.0 - 0.0065 * altitude_m) / 293.0) ** 5.26)


# ---------------------------------------------------------------------------
# ET₀ today sensor
# ---------------------------------------------------------------------------

class ET0TodaySensor(SensorEntity, RestoreEntity):
    """
    Sensor: et0_irrigation_et0_today

    Calculates today's accumulated ET₀ (mm) using Penman-Monteith FAO-56.
    Updates every SCAN_INTERVAL using current instantaneous sensor readings
    and the irradiation accumulated so far today.
    """

    _attr_name = "ET₀ Today"
    _attr_unique_id = "et0_irrigation_et0_today"
    _attr_native_unit_of_measurement = "mm"
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:weather-sunny"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, config: dict) -> None:
        self.hass = hass
        self._config = config
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._last_valid_inputs: dict[str, float] = {}

    async def async_added_to_hass(self) -> None:
        """Restore last known ET0 value to avoid unknown after restart/reload."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return

        try:
            restored = float(last_state.state)
        except (ValueError, TypeError):
            return

        if math.isfinite(restored):
            self._attr_native_value = round(restored, 2)

    async def async_update(self) -> None:
        config = self._config
        now_local = dt_util.now()
        attempt_iso = now_local.isoformat()
        self._attr_extra_state_attributes.setdefault("last_successful_update", None)
        self._attr_extra_state_attributes["last_update_attempt"] = attempt_iso

        try:
            # --- Current instantaneous readings ---
            t_state = self.hass.states.get(config["sensor_temperature"])
            rh_state = self.hass.states.get(config["sensor_humidity"])
            wind_state = self.hass.states.get(config["sensor_wind_speed"])
            pressure_state = self.hass.states.get(config["sensor_pressure"])
            altitude_m = _resolve_altitude_m(self.hass, config)

            t = _safe_float(t_state)
            rh = _safe_float(rh_state)
            wind = _safe_float(wind_state)
            pressure = _safe_float(pressure_state)

            invalid_inputs = []
            invalid_details = []
            used_fallback = False
            pressure_from_last_valid = False

            if t is None:
                t = self._last_valid_inputs.get("temperature")
                used_fallback = used_fallback or t is not None
                invalid_inputs.append(config["sensor_temperature"])
                invalid_details.append(
                    f"{config['sensor_temperature']}={getattr(t_state, 'state', 'missing')}"
                )
            else:
                self._last_valid_inputs["temperature"] = t

            if rh is None:
                rh = self._last_valid_inputs.get("humidity")
                used_fallback = used_fallback or rh is not None
                invalid_inputs.append(config["sensor_humidity"])
                invalid_details.append(
                    f"{config['sensor_humidity']}={getattr(rh_state, 'state', 'missing')}"
                )
            else:
                self._last_valid_inputs["humidity"] = rh

            if wind is None:
                wind = self._last_valid_inputs.get("wind")
                used_fallback = used_fallback or wind is not None
                invalid_inputs.append(config["sensor_wind_speed"])
                invalid_details.append(
                    f"{config['sensor_wind_speed']}={getattr(wind_state, 'state', 'missing')}"
                )
            else:
                self._last_valid_inputs["wind"] = wind

            if pressure is None:
                pressure = self._last_valid_inputs.get("pressure")
                pressure_from_last_valid = pressure is not None
                used_fallback = used_fallback or pressure is not None
                invalid_inputs.append(config["sensor_pressure"])
                invalid_details.append(
                    f"{config['sensor_pressure']}={getattr(pressure_state, 'state', 'missing')}"
                )
            else:
                self._last_valid_inputs["pressure"] = pressure

            if pressure is None:
                pressure = _pressure_from_altitude_kpa(altitude_m)
                used_fallback = True
                pressure_source = "altitude_estimate"
            else:
                pressure_source = "last_valid" if pressure_from_last_valid else "sensor"

            if any(v is None for v in [t, rh, wind]):
                self._attr_extra_state_attributes["last_update_error"] = (
                    "missing_inputs_no_fallback"
                )
                self._attr_extra_state_attributes["invalid_inputs"] = invalid_details
                _LOGGER.warning(
                    "ET₀ Today: skipping update because required inputs are unavailable and no fallback exists. Invalid sensors: %s. Keeping last valid value: %s",
                    ", ".join(invalid_details) or "unknown",
                    self._attr_native_value,
                )
                return

            t = _temperature_to_celsius(t, config.get("temperature_unit", "°C"))

            # Convert units
            u2 = wind / 3.6 if config.get("wind_speed_unit", "km/h") == "km/h" else wind
            if pressure_source == "altitude_estimate":
                pressure_kpa = pressure
            else:
                pressure_kpa = (
                    pressure / 10.0 if config.get("pressure_unit", "hPa") == "hPa" else pressure
                )

            # Irradiation accumulated today (Wh/m²)
            rs = await _daily_irradiation_wh_m2(
                self.hass, config["sensor_luminosity"], now_local
            )

            lat = self.hass.config.latitude
            doy = now_local.timetuple().tm_yday
            et0 = _penman_monteith(t, rh, u2, rs, pressure_kpa, lat, doy)

            self._attr_native_value = round(et0, 2)

            # NOTE: et0_daily_history is intentionally NOT updated here.
            # This sensor polls every 30 min with today's *accumulated* value.
            # The history is populated once per day by WaterDeficitSensor at
            # midnight rollover, so the deque holds one closed-day value per day.

            self._attr_extra_state_attributes = {
                "temperature_c": round(t, 1),
                "humidity_pct": round(rh, 1),
                "wind_speed_ms": round(u2, 2),
                "pressure_kpa": round(pressure_kpa, 2),
                "altitude_m": round(altitude_m, 1),
                "pressure_source": pressure_source,
                "irradiation_wh_m2": round(rs, 0),
                "used_input_fallback": used_fallback,
                "fallback_inputs": invalid_inputs,
                "last_update_attempt": attempt_iso,
                "last_successful_update": attempt_iso,
                "last_update_error": None,
            }

            # Notify all deficit sensors in parallel (flat fan-out):
            #   ET₀TodaySensor -> WaterDeficitSensor
            #                   -> ZoneWaterDeficitSensor (A, B, C...)
            water_deficit_entity = self._config.get("water_deficit_1d_entity")
            if water_deficit_entity is not None:
                try:
                    await water_deficit_entity.async_on_et0_updated()
                except Exception:
                    _LOGGER.exception(
                        "ET₀ Today: error notifying WaterDeficitSensor"
                    )
            for zone_entity in self._config.get("zone_deficit_entities", []):
                try:
                    await zone_entity.async_on_et0_updated()
                except Exception:
                    _LOGGER.exception(
                        "ET₀ Today: error notifying zone entity %s",
                        getattr(zone_entity, "entity_id", repr(zone_entity)),
                    )
        except Exception:
            self._attr_extra_state_attributes["last_update_error"] = "unexpected_exception"
            _LOGGER.exception(
                "ET₀ Today: unexpected error during update. Keeping last valid value: %s",
                self._attr_native_value,
            )


# ---------------------------------------------------------------------------
# Water deficit sensor (N days)
# ---------------------------------------------------------------------------

class WaterDeficitSensor(SensorEntity):
    """
    Sensor: et0_irrigation_deficit_Nd

    Daily global deficit (mm):
        deficit = ET₀_today − rain_today

    The value is intraday and naturally resets at midnight because ET₀ Today
    and rain_today restart for the new day.

    Updated via push from ET0TodaySensor (not by polling).

    Positive value → soil moisture deficit → irrigate.
    Negative value → surplus → skip irrigation.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent"
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        config: dict,
        days: int,
    ) -> None:
        self.hass = hass
        self._config = config
        self._days = days
        self._attr_name = f"Water Deficit {days}d"
        self._attr_unique_id = f"et0_irrigation_deficit_{days}d"
        self._attr_native_unit_of_measurement = "mm"
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

        # Keep only previous-day context used by legacy attribute
        # previous_day_rained and ET0 rolling history for surplus floor.
        self._yesterday_et0: float = 0.0
        self._yesterday_rain: float = 0.0
        self._yesterday_rained: bool = False
        self._last_closed_day_processed: date | None = None

    async def async_added_to_hass(self) -> None:
        """Populate previous-day cache on startup so first push has full attributes."""
        await super().async_added_to_hass()
        await self._async_rebuild_closed_days_cache()
        try:
            await self.async_on_et0_updated()
        except Exception:
            _LOGGER.exception(
                "Water Deficit %dd: failed to publish initial state; using zero fallback",
                self._days,
            )
            self._attr_native_value = 0.0
            self._attr_extra_state_attributes = {
                "total_et0_mm": 0.0,
                "total_rain_mm": 0.0,
                "intraday_et0_mm": 0.0,
                "intraday_rain_mm": 0.0,
                "intraday_deficit_mm": 0.0,
                "days": self._days,
                "daily_detail": [],
                "previous_day_rained": self._yesterday_rained,
                "yesterday_et0_mm": round(self._yesterday_et0, 2),
                "yesterday_rain_mm": round(self._yesterday_rain, 2),
            }
            self.async_write_ha_state()

    async def _async_rebuild_closed_days_cache(self) -> None:
        """Refresh previous-day ET0/rain context and ET0 rolling history."""
        config = self._config
        now_local = dt_util.now()
        yesterday = (now_local - timedelta(days=1)).date()
        lat = self.hass.config.latitude
        altitude_m = _resolve_altitude_m(self.hass, config)
        wind_unit = config.get("wind_speed_unit", "km/h")
        pressure_unit = config.get("pressure_unit", "hPa")

        day_dt = datetime(
            yesterday.year,
            yesterday.month,
            yesterday.day,
            tzinfo=dt_util.get_time_zone(self.hass.config.time_zone),
        )
        doy = day_dt.timetuple().tm_yday

        t_stats = await _daily_stats(self.hass, config["sensor_temperature"], day_dt)
        rh_stats = await _daily_stats(self.hass, config["sensor_humidity"], day_dt)
        wind_stats = await _daily_stats(self.hass, config["sensor_wind_speed"], day_dt)
        pressure_stats = await _daily_stats(self.hass, config["sensor_pressure"], day_dt)
        rain_stats = await _daily_stats(self.hass, config["sensor_rain_today"], day_dt)

        t_mean = t_stats["mean"]
        rh_mean = rh_stats["mean"]
        wind_mean = wind_stats["mean"]
        pressure_mean = pressure_stats["mean"]

        yesterday_et0 = 0.0
        yesterday_rain = float(rain_stats["max"] or 0.0)

        if not any(v is None for v in [t_mean, rh_mean, wind_mean]):
            t_mean = _temperature_to_celsius(t_mean, config.get("temperature_unit", "°C"))
            u2 = wind_mean / 3.6 if wind_unit == "km/h" else wind_mean
            pressure_kpa = (
                _pressure_from_altitude_kpa(altitude_m)
                if pressure_mean is None
                else (pressure_mean / 10.0 if pressure_unit == "hPa" else pressure_mean)
            )
            rs = await _daily_irradiation_wh_m2(self.hass, config["sensor_luminosity"], day_dt)
            yesterday_et0 = _penman_monteith(t_mean, rh_mean, u2, rs, pressure_kpa, lat, doy)
        else:
            _LOGGER.warning(
                "Water Deficit %dd: missing data for %s, keeping yesterday ET0 as 0.0",
                self._days,
                yesterday,
            )

        last_history_day = config.get("et0_history_last_day")
        if last_history_day != yesterday.isoformat():
            config["et0_daily_history"].append(round(yesterday_et0, 2))
            config["et0_history_last_day"] = yesterday.isoformat()

        self._yesterday_et0 = yesterday_et0
        self._yesterday_rain = yesterday_rain
        self._yesterday_rained = yesterday_rain >= yesterday_et0
        self._last_closed_day_processed = yesterday

    async def async_on_et0_updated(self) -> None:
        """Called by ET0TodaySensor after each successful ET₀ calculation.

        Checks whether the day has rolled over (midnight crossed since last
        call) and rebuilds the closed-day cache if so.  Then combines the
        cached closed-day totals with the live intraday readings to produce
        the current deficit, writes HA state, and propagates to zone sensors.
        """
        now_local = dt_util.now()
        yesterday = (now_local - timedelta(days=1)).date()

        # Rebuild closed-day cache if we've crossed midnight since last rebuild.
        if self._last_closed_day_processed != yesterday:
            await self._async_rebuild_closed_days_cache()

        # Daily reference deficit: ET₀_today - rain_today
        et0_entity = self._config.get("et0_today_entity")
        et0_today = float(getattr(et0_entity, "native_value", None) or 0.0)

        rain_state = self.hass.states.get(self._config.get(CONF_SENSOR_RAIN_TODAY, ""))
        rain_today = _safe_float(rain_state) or 0.0

        deficit = et0_today - rain_today

        self._attr_native_value = round(deficit, 2)
        self._attr_extra_state_attributes = {
            "total_et0_mm": round(et0_today, 2),
            "total_rain_mm": round(rain_today, 2),
            "intraday_et0_mm": round(et0_today, 2),
            "intraday_rain_mm": round(rain_today, 2),
            "intraday_deficit_mm": round(deficit, 2),
            "days": self._days,
            "daily_detail": [],
            # True when yesterday's rain covered or exceeded ET0.
            # Used by the generated automation to skip irrigation entirely.
            "previous_day_rained": self._yesterday_rained,
            "yesterday_et0_mm": round(self._yesterday_et0, 2),
            "yesterday_rain_mm": round(self._yesterday_rain, 2),
        }
        self.async_write_ha_state()


async def _daily_switch_on_minutes(
    hass: HomeAssistant,
    switch_entity_id: str,
    date: datetime,
) -> float:
    """Return minutes the zone switch stayed ON during the given day."""
    local_tz = dt_util.get_time_zone(hass.config.time_zone)
    day_start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=local_tz)
    day_end = day_start + timedelta(days=1)

    def _fetch():
        return get_significant_states(
            hass,
            day_start,
            day_end,
            [switch_entity_id],
            significant_changes_only=False,
        )

    history = await get_instance(hass).async_add_executor_job(_fetch)
    states = history.get(switch_entity_id, [])
    if not states:
        return 0.0

    states = sorted(states, key=lambda s: s.last_changed)

    first = states[0]
    prev_time = day_start
    prev_state = "off"
    if first.last_changed <= day_start:
        prev_state = first.state

    on_seconds = 0.0
    for s in states:
        ts = s.last_changed
        if ts < day_start:
            prev_state = s.state
            prev_time = day_start
            continue
        if ts > day_end:
            ts = day_end

        if prev_state == "on" and ts > prev_time:
            on_seconds += (ts - prev_time).total_seconds()

        prev_state = s.state
        prev_time = ts

    if prev_state == "on" and day_end > prev_time:
        on_seconds += (day_end - prev_time).total_seconds()

    return on_seconds / 60.0


class ZoneWaterDeficitSensor(SensorEntity, RestoreEntity):
    """Zone water deficit with intra-day environmental updates.

    Update rules:
    - Intra-day (polling): apply delta of (ET0_today * factor - rain_today)
    - At day rollover: add previous day's ambient deficit (zone ET0 - rain)
    - At irrigation end (switch off): subtract effective irrigation depth (mm)
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sprinkler-variant"
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        config: dict,
        zone: dict,
    ) -> None:
        self.hass = hass
        self._config = config
        self._zone = zone
        self._switch = str(zone.get(CONF_ZONE_SWITCH))
        self._days = max(0, min(5, int(zone.get(CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION, 0) or 0)))
        self._factor = float(zone.get(CONF_ZONE_FACTOR, 1.0))
        self._zone_created_at = str(zone.get(CONF_ZONE_CREATED_AT, "") or "")
        # Dynamic floor will be computed at update time based on ambient ET0 average.
        self._min_surplus_floor = self._compute_surplus_floor()
        try:
            self._application_rate = float(zone.get(CONF_ZONE_APPLICATION_RATE, DEFAULT_APPLICATION_RATE))
        except (TypeError, ValueError):
            self._application_rate = DEFAULT_APPLICATION_RATE
        if self._application_rate <= 0:
            self._application_rate = DEFAULT_APPLICATION_RATE

        friendly_name = zone.get("switch_friendly_name") or self._switch
        self._attr_name = f"Zone Deficit {friendly_name}"
        self._attr_unique_id = f"et0_irrigation_zone_deficit_{_zone_slug(self._switch)}"
        self.entity_id = _zone_deficit_entity_id(self._switch)
        self._attr_native_unit_of_measurement = "mm"
        self._attr_native_value: float = 0.0
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._last_processed_day: date | None = None
        self._last_effective_watering_day: date | None = None
        self._switch_on_started_at: datetime | None = None
        self._last_environment_source: str | None = None
        self._intraday_day: date | None = None
        self._intraday_balance_mm: float | None = None
        self._unsub_switch_listener = None

    async def async_added_to_hass(self) -> None:
        """Restore state and start listeners."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        restored_created_at = ""
        if last_state is not None:
            raw_created_at = last_state.attributes.get("zone_created_at")
            if isinstance(raw_created_at, str):
                restored_created_at = raw_created_at

        should_reset_to_zero = (
            last_state is None
            or (
                bool(self._zone_created_at)
                and self._zone_created_at != restored_created_at
            )
        )

        if should_reset_to_zero:
            # New zone sensor: start from zero and only accumulate going forward.
            self._attr_native_value = 0.0
            self._last_processed_day = (dt_util.now() - timedelta(days=1)).date()
            self._last_effective_watering_day = dt_util.now().date()
            self._last_environment_source = "initial_zero_new_or_readded_zone"
        else:
            try:
                restored = float(last_state.state)
                if math.isfinite(restored):
                    self._attr_native_value = round(restored, 2)
            except (TypeError, ValueError):
                pass

            restored_day = last_state.attributes.get("last_processed_day")
            if isinstance(restored_day, str):
                try:
                    self._last_processed_day = datetime.fromisoformat(restored_day).date()
                except ValueError:
                    self._last_processed_day = None

            restored_watering_day = last_state.attributes.get("last_effective_watering_day")
            if isinstance(restored_watering_day, str):
                try:
                    self._last_effective_watering_day = datetime.fromisoformat(restored_watering_day).date()
                except ValueError:
                    self._last_effective_watering_day = None

            restored_intraday_day = last_state.attributes.get("intraday_day")
            if isinstance(restored_intraday_day, str):
                try:
                    self._intraday_day = datetime.fromisoformat(restored_intraday_day).date()
                except ValueError:
                    self._intraday_day = None

            restored_intraday_balance = last_state.attributes.get("intraday_balance_mm")
            try:
                if restored_intraday_balance is not None:
                    self._intraday_balance_mm = float(restored_intraday_balance)
            except (TypeError, ValueError):
                self._intraday_balance_mm = None

        # If switch is currently ON, consider its current ON run as active.
        current_switch_state = self.hass.states.get(self._switch)
        if current_switch_state is not None and current_switch_state.state == "on":
            self._switch_on_started_at = current_switch_state.last_changed

        self._unsub_switch_listener = async_track_state_change_event(
            self.hass,
            [self._switch],
            self._async_handle_switch_event,
        )

        await self._async_process_pending_days()
        await self._async_apply_intraday_environment_delta(initializing=True)
        self._update_attrs()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_switch_listener is not None:
            self._unsub_switch_listener()
            self._unsub_switch_listener = None
        await super().async_will_remove_from_hass()

    def _intraday_environment_balance(self) -> float | None:
        """Return current intra-day environmental balance for this zone.

        balance_mm = (ET0_today_mm * zone_factor) - rain_today_mm
        """
        et0_entity = self._config.get("et0_today_entity")
        et0_today = getattr(et0_entity, "native_value", None)
        try:
            et0_today_mm = float(et0_today)
        except (TypeError, ValueError):
            return None

        rain_state = self.hass.states.get(self._config.get(CONF_SENSOR_RAIN_TODAY, ""))
        rain_today_mm = _safe_float(rain_state)
        if rain_today_mm is None:
            rain_today_mm = 0.0

        return (et0_today_mm * self._factor) - rain_today_mm

    async def _async_apply_intraday_environment_delta(self, initializing: bool = False) -> None:
        """Apply intra-day delta to keep zone deficit updated at poll frequency."""
        now_local = dt_util.now()
        today = now_local.date()
        current_balance = self._intraday_environment_balance()
        if current_balance is None:
            return

        if self._intraday_day is None or self._intraday_balance_mm is None:
            self._intraday_day = today
            self._intraday_balance_mm = current_balance
            if initializing:
                self._last_environment_source = "intraday_baseline"
            return

        if self._intraday_day != today:
            if self._last_processed_day is None or self._intraday_day > self._last_processed_day:
                self._last_processed_day = self._intraday_day
            self._intraday_day = today
            self._intraday_balance_mm = current_balance
            self._last_environment_source = "intraday_day_rollover"
            return

        delta_mm = current_balance - self._intraday_balance_mm
        if abs(delta_mm) < 1e-6:
            return

        self._attr_native_value = round(self._attr_native_value + delta_mm, 2)
        self._apply_surplus_floor()
        self._intraday_balance_mm = current_balance
        self._last_environment_source = "intraday_et0_today_rain_today"

    async def _async_process_pending_days(self, reference: datetime | None = None) -> None:
        """Apply ambient deficits for all missing closed days since last processing."""
        now_local = reference or dt_util.now()
        yesterday = (now_local - timedelta(days=1)).date()
        if self._last_processed_day is None:
            # Bootstrap from configured drought tolerance window.
            start_day = yesterday - timedelta(days=self._days - 1)
        else:
            start_day = self._last_processed_day + timedelta(days=1)

        if start_day > yesterday:
            return

        cursor = start_day
        while cursor <= yesterday:
            day_dt = datetime(cursor.year, cursor.month, cursor.day, tzinfo=dt_util.get_time_zone(self.hass.config.time_zone))
            daily_deficit = await self._async_daily_environment_deficit(day_dt)
            self._attr_native_value = round(self._attr_native_value + daily_deficit, 2)
            self._apply_surplus_floor()
            if await self._async_day_counts_as_watering(day_dt):
                self._last_effective_watering_day = cursor
            self._last_processed_day = cursor
            cursor += timedelta(days=1)

    async def _async_day_counts_as_watering(self, day: datetime) -> bool:
        """Return True when rain covered ET0 for the given day (treated like irrigation)."""
        config = self._config
        wind_unit = config.get("wind_speed_unit", "km/h")
        pressure_unit = config.get("pressure_unit", "hPa")

        t_stats = await _daily_stats(self.hass, config["sensor_temperature"], day)
        rh_stats = await _daily_stats(self.hass, config["sensor_humidity"], day)
        wind_stats = await _daily_stats(self.hass, config["sensor_wind_speed"], day)
        pressure_stats = await _daily_stats(self.hass, config["sensor_pressure"], day)
        rain_stats = await _daily_stats(self.hass, config["sensor_rain_today"], day)

        t_mean = t_stats["mean"]
        rh_mean = rh_stats["mean"]
        wind_mean = wind_stats["mean"]
        pressure_mean = pressure_stats["mean"]

        if any(v is None for v in [t_mean, rh_mean, wind_mean, pressure_mean]):
            return False

        t_mean = _temperature_to_celsius(t_mean, config.get("temperature_unit", "°C"))
        u2 = wind_mean / 3.6 if wind_unit == "km/h" else wind_mean
        altitude_m = _resolve_altitude_m(self.hass, config)
        pressure_kpa = (
            _pressure_from_altitude_kpa(altitude_m)
            if pressure_mean is None
            else (pressure_mean / 10.0 if pressure_unit == "hPa" else pressure_mean)
        )

        rs = await _daily_irradiation_wh_m2(self.hass, config["sensor_luminosity"], day)
        doy = day.timetuple().tm_yday
        et0_day = _penman_monteith(t_mean, rh_mean, u2, rs, pressure_kpa, self.hass.config.latitude, doy)
        zone_et0_day = et0_day * self._factor
        rain_day = rain_stats["max"] or 0.0
        return rain_day >= zone_et0_day

    async def _async_daily_environment_deficit(self, day: datetime) -> float:
        """Return zone ambient deficit for one closed day.

        Recompute from weather history for the specific closed day.
        """
        self._last_environment_source = "fallback_recalculation"
        config = self._config
        wind_unit = config.get("wind_speed_unit", "km/h")
        pressure_unit = config.get("pressure_unit", "hPa")

        t_stats = await _daily_stats(self.hass, config["sensor_temperature"], day)
        rh_stats = await _daily_stats(self.hass, config["sensor_humidity"], day)
        wind_stats = await _daily_stats(self.hass, config["sensor_wind_speed"], day)
        pressure_stats = await _daily_stats(self.hass, config["sensor_pressure"], day)
        rain_stats = await _daily_stats(self.hass, config["sensor_rain_today"], day)

        t_mean = t_stats["mean"]
        rh_mean = rh_stats["mean"]
        wind_mean = wind_stats["mean"]
        pressure_mean = pressure_stats["mean"]

        if any(v is None for v in [t_mean, rh_mean, wind_mean, pressure_mean]):
            return 0.0

        t_mean = _temperature_to_celsius(t_mean, config.get("temperature_unit", "°C"))
        u2 = wind_mean / 3.6 if wind_unit == "km/h" else wind_mean
        altitude_m = _resolve_altitude_m(self.hass, config)
        pressure_kpa = (
            _pressure_from_altitude_kpa(altitude_m)
            if pressure_mean is None
            else (pressure_mean / 10.0 if pressure_unit == "hPa" else pressure_mean)
        )

        rs = await _daily_irradiation_wh_m2(self.hass, config["sensor_luminosity"], day)
        doy = day.timetuple().tm_yday
        et0_day = _penman_monteith(t_mean, rh_mean, u2, rs, pressure_kpa, self.hass.config.latitude, doy)
        zone_et0_day = et0_day * self._factor
        rain_day = rain_stats["max"] or 0.0
        return zone_et0_day - rain_day

    async def _async_handle_switch_event(self, event) -> None:
        """Track ON/OFF transitions to subtract effective irrigation depth."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return

        old = old_state.state if old_state is not None else None
        new = new_state.state

        if new == "on" and old != "on":
            self._switch_on_started_at = new_state.last_changed
            self._update_attrs(last_irrigation_mm=0.0)
            self.async_write_ha_state()
            return

        if old == "on" and new != "on":
            started_at = self._switch_on_started_at or (old_state.last_changed if old_state is not None else None)
            self._switch_on_started_at = None
            if started_at is None:
                return

            ended_at = new_state.last_changed
            duration_min = max(0.0, (ended_at - started_at).total_seconds() / 60.0)
            irrig_mm = duration_min * self._application_rate
            deficit_before = float(self._attr_native_value)
            self._attr_native_value = round(self._attr_native_value - irrig_mm, 2)
            self._apply_surplus_floor()
            _LOGGER.debug(
                "Zone deficit update on OFF | switch=%s factor=%.3f rate_mm_min=%.3f "
                "duration_min=%.2f irrig_mm=%.2f deficit_before=%.2f deficit_after=%.2f",
                self._switch,
                self._factor,
                self._application_rate,
                duration_min,
                irrig_mm,
                deficit_before,
                float(self._attr_native_value),
            )
            if irrig_mm > 0:
                self._last_effective_watering_day = ended_at.date()
            self._update_attrs(last_irrigation_mm=irrig_mm)
            self.async_write_ha_state()

    async def async_reset_deficit(self) -> None:
        """Reset zone deficit to zero and start counting from now."""
        now_local = dt_util.now()
        self._attr_native_value = 0.0
        self._apply_surplus_floor()
        self._last_processed_day = (now_local - timedelta(days=1)).date()
        self._last_effective_watering_day = now_local.date()
        self._last_environment_source = "manual_reset"
        self._switch_on_started_at = None
        self._intraday_day = now_local.date()
        self._intraday_balance_mm = self._intraday_environment_balance()
        self._update_attrs(last_irrigation_mm=0.0)
        self.async_write_ha_state()

    def _compute_surplus_floor(self) -> float:
        """Calculate dynamic surplus floor from ambient ET0 average and zone factor.
        
        floor = -ET0_average * zone_factor * max_days_without_irrigation
        
        If insufficient history (< 1 day), fallback to constant 5mm.
        """
        et0_history = self._config.get("et0_daily_history", deque())
        if len(et0_history) >= 1:
            et0_avg = sum(et0_history) / len(et0_history)
            floor = -1.0 * et0_avg * self._factor * self._days
            return round(floor, 2)
        # Fallback: use default constant while building history.
        return -1.0 * DEFAULT_MIN_SURPLUS_FLOOR_MM_PER_DAY * self._factor * self._days

    def _apply_surplus_floor(self) -> None:
        """Enforce minimum surplus floor to cap negative deficit (prevent excessive carryover)."""
        # Recompute floor dynamically as ET0 average evolves.
        self._min_surplus_floor = self._compute_surplus_floor()
        if self._attr_native_value < self._min_surplus_floor:
            self._attr_native_value = round(self._min_surplus_floor, 2)

    def _days_without_irrigation(self, reference: datetime | None = None) -> int:
        """Return number of full days since the last effective watering event."""
        today = (reference or dt_util.now()).date()
        if self._last_effective_watering_day is None:
            return self._days
        return max(0, (today - self._last_effective_watering_day).days)

    def _update_attrs(self, last_irrigation_mm: float | None = None) -> None:
        self._attr_extra_state_attributes = {
            "days": self._days,
            "zone_switch": self._switch,
            "zone_type": self._zone.get(CONF_ZONE_TYPE),
            "zone_created_at": self._zone_created_at or None,
            "et0_factor": self._factor,
            "application_rate_mm_min": round(self._application_rate, 3),
            "min_surplus_floor_mm": round(self._min_surplus_floor, 2),
            "et0_history_days": len(self._config.get("et0_daily_history", deque())),
            "et0_average_basis": "historical" if len(self._config.get("et0_daily_history", deque())) >= 7 else "building",
            "last_processed_day": self._last_processed_day.isoformat() if self._last_processed_day else None,
            "last_effective_watering_day": self._last_effective_watering_day.isoformat() if self._last_effective_watering_day else None,
            "intraday_day": self._intraday_day.isoformat() if self._intraday_day else None,
            "intraday_balance_mm": round(self._intraday_balance_mm, 3) if self._intraday_balance_mm is not None else None,
            "days_without_irrigation": self._days_without_irrigation(),
            "environment_source": self._last_environment_source,
            "switch_currently_on": self._switch_on_started_at is not None,
            "last_irrigation_mm": round(last_irrigation_mm, 2) if last_irrigation_mm is not None else None,
        }

    async def async_on_et0_updated(self) -> None:
        """Called by ET0TodaySensor after each successful ET₀ calculation.

        Replaces polling: zones are updated synchronously after ET₀ so the
        intraday delta is always computed against a freshly calculated value,
        eliminating the race condition where ET₀ Today still held the previous
        day's accumulated value at midnight rollover.
        """
        # Intra-day update first to roll over day baseline and avoid double-counting
        # in pending closed-day processing.
        await self._async_apply_intraday_environment_delta()
        await self._async_process_pending_days()
        self._update_attrs()
        self.async_write_ha_state()
