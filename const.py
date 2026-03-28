"""Constants for ET₀ Irrigation."""

DOMAIN = "et0_irrigation"

# Config keys — weather sensors
CONF_SENSOR_TEMPERATURE = "sensor_temperature"
CONF_TEMPERATURE_UNIT = "temperature_unit"
CONF_SENSOR_HUMIDITY = "sensor_humidity"
CONF_SENSOR_WIND_SPEED = "sensor_wind_speed"
CONF_SENSOR_LUMINOSITY = "sensor_luminosity"
CONF_SENSOR_PRESSURE = "sensor_pressure"
CONF_SENSOR_RAIN_TODAY = "sensor_rain_today"
CONF_WIND_SPEED_UNIT = "wind_speed_unit"
CONF_PRESSURE_UNIT = "pressure_unit"
CONF_HISTORY_DAYS = "history_days"

# Config keys — general irrigation parameters
CONF_IRRIGATION_TIME = "irrigation_time"       # HH:MM
CONF_MIN_DEFICIT = "min_deficit"               # mm
CONF_DEFICIT_SENSOR_DAYS = "deficit_sensor_days"  # 1..7

# Config keys — zones
CONF_ZONES = "zones"
CONF_ZONE_NAME = "name"
CONF_ZONE_CREATED_AT = "created_at"
CONF_ZONE_SWITCH = "switch"
CONF_ZONE_TYPE = "type"                        # "et0" | "fixed"
CONF_ZONE_FACTOR = "factor"                    # float 0.1–2.0 (ET adjustment, all zones)
CONF_ZONE_FIXED_MINUTES = "fixed_minutes"      # int (fixed zones)
CONF_ZONE_APPLICATION_RATE = "application_rate"  # float mm/min (zone-specific)
CONF_ZONE_MAX_DAYS_WITHOUT_IRRIGATION = "max_days_without_irrigation"  # int 0..5
CONF_ZONE_REQUIRES_COMPANION = "requires_companion"  # bool
CONF_ZONE_COMPANION_POOL = "companion_pool"          # list of zone names/switch ids

# Config keys — blocks
CONF_BLOCKS = "blocks"
CONF_BLOCK_ZONES = "zones"                     # list of zone names in this block
CONF_BLOCK_REFERENCE_ZONE = "reference_zone"   # legacy key (kept for compatibility)

# Automation unique ID stored in entry options so we can remove it later
AUTOMATION_UNIQUE_ID_KEY = "managed_automation_id"

# Application rate for Rain Bird XF-SDI with 30cm × 40cm spacing (mm/min)
DEFAULT_APPLICATION_RATE = 0.473

# Minimum surplus floor (negative deficit limit) to prevent excessive water carryover.
# Expressed as base mm per max day without irrigation.
# E.g., zone with max_days_without_irrigation=3 → floor = -5mm * 3 = -15mm
DEFAULT_MIN_SURPLUS_FLOOR_MM_PER_DAY = 5.0
