"""真实水情数据源适配器框架。

目前未接入经过验证的成都官方实时水文 API（不伪造、不请求不存在的接口）。
当配置了 WATER_DATA_API_URL 后，适配器会按预留结构执行请求；
在未配置之前，所有读取方法都会抛出 ProviderNotConfiguredError，
由上层 DataCollector 自动 fallback 到模拟数据。
"""

import os

from .water_data_provider import ProviderNotConfiguredError, WaterDataProvider


class RealWaterProvider(WaterDataProvider):
    name = "official_api"
    display_name = "官方实时数据"

    def __init__(self):
        self.api_url = os.environ.get("WATER_DATA_API_URL", "").strip()
        self.api_key = os.environ.get("WATER_DATA_API_KEY", "").strip()

    def is_configured(self) -> bool:
        return bool(self.api_url)

    def _check_configured(self):
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                "真实数据源尚未配置：请设置 WATER_DATA_API_URL 环境变量"
            )

    def _fetch_json(self, path: str = "") -> dict:
        """预留：未来在这里向真实 API 发起请求。

        注意：在 WATER_DATA_API_URL 被验证可用之前，本方法不会真正发出请求。
        """
        self._check_configured()
        # TODO: 接入真实水文 API 时，在此实现 HTTP 请求、鉴权与响应解析。
        # 示例接口返回结构（仅占位，不请求任何真实地址）：
        # {
        #   "stations": [
        #     {"station_id": "...", "station_name": "...",
        #      "timestamp": "...", "water_level": 0.0, "warning_level": 0.0,
        #      "rainfall": 0.0, "status": "...", "latitude": 0.0, "longitude": 0.0}
        #   ]
        # }
        raise ProviderNotConfiguredError(
            "真实数据源未完成适配，WATER_DATA_API_URL 对应的接口尚未验证"
        )

    def get_stations(self) -> list:
        self._fetch_json(path="stations")

    def get_current_data(self) -> list:
        payload = self._fetch_json(path="current")
        return (payload.get("stations") or []) if isinstance(payload, dict) else []

    def get_history_data(self, station_id: str, limit: int = 20) -> list:
        self._fetch_json(path=f"history/{station_id}")