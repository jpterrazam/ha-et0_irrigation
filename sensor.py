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
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.util import dt as dt_util

from . import DOMAIN
from .const import (
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

    entities = [
        ET0TodaySensor(hass, entry, config),
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

            t = _safe_float(t_state)
            rh = _safe_float(rh_state)
            wind = _safe_float(wind_state)
            pressure = _safe_float(pressure_state)

            invalid_inputs = []
            invalid_details = []
            used_fallback = False

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
                used_fallback = used_fallback or pressure is not None
                invalid_inputs.append(config["sensor_pressure"])
                invalid_details.append(
                    f"{config['sensor_pressure']}={getattr(pressure_state, 'state', 'missing')}"
                )
            else:
                self._last_valid_inputs["pressure"] = pressure

            if any(v is None for v in [t, rh, wind, pressure]):
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
            pressure_kpa = pressure / 10.0 if config.get("pressure_unit", "hPa") == "hPa" else pressure

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
                "irradiation_wh_m2": round(rs, 0),
                "used_input_fallback": used_fallback,
                "fallback_inputs": invalid_inputs,
                "last_update_attempt": attempt_iso,
                "last_successful_update": attempt_iso,
                "last_update_error": None,
            }
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

    Water deficit over the last N complete days (mm).
    deficit = Σ ET₀(d) − Σ rain(d)   for d in [today-N .. today-1]

    Positive value → soil moisture deficit → irrigate.
    Negative value → surplus → skip irrigation.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent"

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

    async def async_update(self) -> None:
        config = self._config
        now_local = dt_util.now()
        lat = self.hass.config.latitude

        wind_unit = config.get("wind_speed_unit", "km/h")
        pressure_unit = config.get("pressure_unit", "hPa")

        total_et0 = 0.0
        total_rain = 0.0
        daily_detail: list[dict] = []

        # yesterday_et0 is captured to push into et0_daily_history once per
        # update cycle (offset=1 is always yesterday — the most recent closed
        # day), fixing bug #2 where ET0TodaySensor was pushing intra-day
        # accumulated values instead of closed-day totals.
        yesterday_et0: float | None = None

        for offset in range(1, self._days + 1):
            day = now_local - timedelta(days=offset)
            doy = day.timetuple().tm_yday

            # Temperature stats
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
                _LOGGER.warning(
                    "Water Deficit %dd: missing data for %s, skipping day",
                    self._days,
                    day.date(),
                )
                continue

            t_mean = _temperature_to_celsius(t_mean, config.get("temperature_unit", "°C"))

            u2 = wind_mean / 3.6 if wind_unit == "km/h" else wind_mean
            pressure_kpa = pressure_mean / 10.0 if pressure_unit == "hPa" else pressure_mean

            rs = await _daily_irradiation_wh_m2(
                self.hass, config["sensor_luminosity"], day
            )

            et0_day = _penman_monteith(t_mean, rh_mean, u2, rs, pressure_kpa, lat, doy)

            # Rain: use max value of the day (rain sensors typically show
            # cumulative daily total, so max = total for that day)
            rain_day = rain_stats["max"] or 0.0

            total_et0 += et0_day
            total_rain += rain_day

            if offset == 1:
                yesterday_et0 = et0_day

            daily_detail.append(
                {
                    "date": str(day.date()),
                    "et0_mm": round(et0_day, 2),
                    "rain_mm": round(rain_day, 2),
                    "deficit_mm": round(et0_day - rain_day, 2),
                }
            )

        # Push yesterday's closed-day ET0 into the rolling history used by
        # ZoneWaterDeficitSensor to compute the dynamic surplus floor.
        if yesterday_et0 is not None:
            config["et0_daily_history"].append(round(yesterday_et0, 2))

        deficit = total_et0 - total_rain
        self._attr_native_value = round(deficit, 2)
        self._attr_extra_state_attributes = {
            "total_et0_mm": round(total_et0, 2),
            "total_rain_mm": round(total_rain, 2),
            "days": self._days,
            "daily_detail": daily_detail,
        }


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
    """Event-driven zone water deficit.

    Update rules:
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
        self._days = max(1, min(5, int(zone.get(CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION, 0) or 0)))
        self._factor = float(zone.get(CONF_ZONE_FACTOR, 1.0))
        self._is_et0_zone = zone.get(CONF_ZONE_TYPE) == "et0"
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
        self._unsub_switch_listener = None
        self._unsub_midnight_listener = None

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

        # If switch is currently ON, consider its current ON run as active.
        current_switch_state = self.hass.states.get(self._switch)
        if current_switch_state is not None and current_switch_state.state == "on":
            self._switch_on_started_at = current_switch_state.last_changed

        self._unsub_switch_listener = async_track_state_change_event(
            self.hass,
            [self._switch],
            self._async_handle_switch_event,
        )
        self._unsub_midnight_listener = async_track_time_change(
            self.hass,
            self._async_handle_day_rollover,
            hour=0,
            minute=5,
            second=0,
        )

        await self._async_process_pending_days()
        self._update_attrs()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_switch_listener is not None:
            self._unsub_switch_listener()
            self._unsub_switch_listener = None
        if self._unsub_midnight_listener is not None:
            self._unsub_midnight_listener()
            self._unsub_midnight_listener = None
        await super().async_will_remove_from_hass()

    async def _async_handle_day_rollover(self, now: datetime) -> None:
        await self._async_process_pending_days(reference=now)
        self._update_attrs()
        self.async_write_ha_state()

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
        pressure_kpa = pressure_mean / 10.0 if pressure_unit == "hPa" else pressure_mean

        rs = await _daily_irradiation_wh_m2(self.hass, config["sensor_luminosity"], day)
        doy = day.timetuple().tm_yday
        et0_day = _penman_monteith(t_mean, rh_mean, u2, rs, pressure_kpa, self.hass.config.latitude, doy)
        rain_day = rain_stats["max"] or 0.0
        return rain_day >= et0_day

    async def _async_daily_environment_deficit(self, day: datetime) -> float:
        """Return zone ambient deficit for one closed day.

        Primary source: native_value of the WaterDeficitSensor(1d) object
        stored in config["water_deficit_1d_entity"] at setup time.  Using
        the object reference avoids the hardcoded "sensor.water_deficit_1d"
        entity_id string, which is fragile when the HA entity registry adds a
        suffix due to name conflicts.
        Fallback source: recompute from weather history for the given day.
        """
        # Prefer the integration's own daily deficit sensor so all zones use
        # the same environmental baseline.
        deficit_entity = self._config.get("water_deficit_1d_entity")
        global_deficit = (
            deficit_entity.native_value
            if deficit_entity is not None
            else None
        )
        if global_deficit is not None:
            try:
                global_deficit = float(global_deficit)
                self._last_environment_source = "water_deficit_1d_entity"
                return global_deficit * self._factor
            except (TypeError, ValueError):
                pass

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
        pressure_kpa = pressure_mean / 10.0 if pressure_unit == "hPa" else pressure_mean

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
            self._attr_native_value = round(self._attr_native_value - irrig_mm, 2)
            self._apply_surplus_floor()
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
            "days_without_irrigation": self._days_without_irrigation(),
            "environment_source": self._last_environment_source,
            "switch_currently_on": self._switch_on_started_at is not None,
            "last_irrigation_mm": round(last_irrigation_mm, 2) if last_irrigation_mm is not None else None,
        }

    async def async_update(self) -> None:
        # Entity is event-driven; this keeps compatibility for manual updates.
        await self._async_process_pending_days()
        self._update_attrs()
