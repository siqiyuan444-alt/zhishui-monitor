from .water_data_provider import (
    STATIONS,
    WATER_RANGES,
    ProviderError,
    ProviderNotConfiguredError,
    WaterDataProvider,
    get_provider,
)
from .data_analysis import (
    analyze_trend,
    forecast,
    risk_level_for,
    DEFAULT_FORECAST_HORIZON,
)

__all__ = [
    "STATIONS",
    "WATER_RANGES",
    "ProviderError",
    "ProviderNotConfiguredError",
    "WaterDataProvider",
    "get_provider",
    "analyze_trend",
    "forecast",
    "risk_level_for",
    "DEFAULT_FORECAST_HORIZON",
]