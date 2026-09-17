"""Open-Meteo 请求规格（Request Specs）。

集中声明每个数据资产向数据源请求哪些变量。此处是"平台期望字段"
与"数据源实际提供字段"的契约边界：若数据源改名或下线变量，
采集器会在校验阶段显式失败，而不是静默产出空列。
"""

from __future__ import annotations

# ============================================================
# Forecast API —— 逐小时气象
# ============================================================
WEATHER_HOURLY_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "snowfall",
    "precipitation_probability",
    "weather_code",
    "cloud_cover",
    "pressure_msl",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "is_day",
    "visibility",
    "uv_index",
)

# ============================================================
# Forecast API —— 逐日气象
# ============================================================
WEATHER_DAILY_VARIABLES: tuple[str, ...] = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "apparent_temperature_max",
    "apparent_temperature_min",
    "precipitation_sum",
    "rain_sum",
    "snowfall_sum",
    "precipitation_hours",
    "precipitation_probability_max",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
    "wind_direction_10m_dominant",
    "shortwave_radiation_sum",
    "et0_fao_evapotranspiration",
    "uv_index_max",
    "sunshine_duration",
    "daylight_duration",
)

# ============================================================
# Archive API —— 历史再分析逐小时气象
# ============================================================
ARCHIVE_HOURLY_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "snowfall",
    "snow_depth",
    "weather_code",
    "cloud_cover",
    "pressure_msl",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "shortwave_radiation",
    "et0_fao_evapotranspiration",
)

# ============================================================
# Air Quality API —— 逐小时空气质量
# ============================================================
AIR_QUALITY_HOURLY_VARIABLES: tuple[str, ...] = (
    "pm2_5",
    "pm10",
    "carbon_monoxide",
    "nitrogen_dioxide",
    "sulphur_dioxide",
    "ozone",
    "ammonia",
    "dust",
    "aerosol_optical_depth",
    "uv_index",
    "uv_index_clear_sky",
    "european_aqi",
    "european_aqi_pm2_5",
    "european_aqi_pm10",
    "european_aqi_nitrogen_dioxide",
    "european_aqi_ozone",
    "european_aqi_sulphur_dioxide",
    "us_aqi",
    "us_aqi_pm2_5",
    "us_aqi_pm10",
    "us_aqi_nitrogen_dioxide",
    "us_aqi_ozone",
    "us_aqi_sulphur_dioxide",
    "us_aqi_carbon_monoxide",
    "alder_pollen",
    "birch_pollen",
    "grass_pollen",
    "mugwort_pollen",
    "olive_pollen",
    "ragweed_pollen",
)

# Archive 数据存在约 5 天发布延迟，回补窗口需避开该区间
ARCHIVE_PUBLICATION_LAG_DAYS = 5

# 平台内部生成的审计/派生列（请求数据源时不应传递）
PLATFORM_COLUMNS: frozenset[str] = frozenset(
    {"city_id", "ingested_at", "source_run_id", "is_forecast", "data_kind"}
)

# 资产键常量，避免各处硬编码字符串
ASSET_FORECAST_HOURLY = "openmeteo.weather.forecast.hourly"
ASSET_FORECAST_DAILY = "openmeteo.weather.forecast.daily"
ASSET_ARCHIVE_HOURLY = "openmeteo.weather.archive.hourly"
ASSET_AIR_QUALITY_HOURLY = "openmeteo.air_quality.hourly"
ASSET_GEOCODING_PLACES = "openmeteo.geocoding.places"
ASSET_ENVIRONMENT_HOURLY = "atmos.city_environment.hourly"
ASSET_ENVIRONMENT_DAILY = "atmos.city_environment.daily"

ALL_ASSET_KEYS: tuple[str, ...] = (
    ASSET_GEOCODING_PLACES,
    ASSET_FORECAST_HOURLY,
    ASSET_FORECAST_DAILY,
    ASSET_ARCHIVE_HOURLY,
    ASSET_AIR_QUALITY_HOURLY,
    ASSET_ENVIRONMENT_HOURLY,
    ASSET_ENVIRONMENT_DAILY,
)

VARIABLES_BY_ASSET: dict[str, tuple[str, ...]] = {
    ASSET_FORECAST_HOURLY: WEATHER_HOURLY_VARIABLES,
    ASSET_FORECAST_DAILY: WEATHER_DAILY_VARIABLES,
    ASSET_ARCHIVE_HOURLY: ARCHIVE_HOURLY_VARIABLES,
    ASSET_AIR_QUALITY_HOURLY: AIR_QUALITY_HOURLY_VARIABLES,
}
