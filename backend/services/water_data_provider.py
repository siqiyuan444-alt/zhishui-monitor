"""统一的水情数据提供器接口。

所有真实/模拟数据源都实现 WaterDataProvider 接口，
上层系统只依赖该接口，从而可以无缝切换数据源。
"""

import os
from abc import ABC, abstractmethod

STATIONS = [
    {"station_id": "ST001", "station_name": "都江堰水文站", "warning_level": 5.0, "latitude": 30.99, "longitude": 103.64},
    {"station_id": "ST002", "station_name": "金堂水文站", "warning_level": 5.5, "latitude": 30.85, "longitude": 104.43},
    {"station_id": "ST003", "station_name": "温江水文站", "warning_level": 4.8, "latitude": 30.70, "longitude": 103.84},
    {"station_id": "ST004", "station_name": "龙泉驿水文站", "warning_level": 6.0, "latitude": 30.56, "longitude": 104.27},
    {"station_id": "ST005", "station_name": "新津水文站", "warning_level": 5.2, "latitude": 30.41, "longitude": 103.81},
]

WATER_RANGES = {
    "ST001": (3.0, 6.0),
    "ST002": (3.5, 6.5),
    "ST003": (2.5, 5.5),
    "ST004": (4.0, 7.0),
    "ST005": (3.2, 6.2),
}


class ProviderError(Exception):
    """数据提供器通用错误。"""


class ProviderNotConfiguredError(ProviderError):
    """真实数据源尚未配置时抛出。"""


class WaterDataProvider(ABC):
    """水情数据提供器抽象接口。"""

    name: str = "unknown"
    display_name: str = "未知数据源"

    @abstractmethod
    def get_stations(self) -> list:
        """返回所有水文站基础信息。"""
        raise NotImplementedError

    @abstractmethod
    def get_current_data(self) -> list:
        """返回所有水文站当前水情数据（原始记录列表）。"""
        raise NotImplementedError

    @abstractmethod
    def get_history_data(self, station_id: str, limit: int = 20) -> list:
        """返回单个水文站的历史数据。"""
        raise NotImplementedError


def get_provider(name: str = None) -> WaterDataProvider:
    """根据配置创建数据提供器实例。

    未指定时读取环境变量 WATER_DATA_PROVIDER（默认 mock）。
    """
    provider_name = (name or os.environ.get("WATER_DATA_PROVIDER", "mock")).strip().lower()

    if provider_name in ("mock", "simulation", "模拟"):
        from .mock_water_provider import MockWaterProvider
        return MockWaterProvider()

    if provider_name in ("official_api", "real", "official", "真实"):
        from .real_water_provider import RealWaterProvider
        return RealWaterProvider()

    raise ProviderError(f"未知的数据提供器配置: {provider_name}")