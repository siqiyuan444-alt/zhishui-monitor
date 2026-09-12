"""数据采集与入库编排服务。

流程：读取配置 → 选择数据提供器 → 采集原始数据 → 标准化 + 质量检查 → 写入 SQLite → 记录采集日志。

- 默认配置为 mock，保证现有网站正常运行；
- 若配置的水情提供器（如 official_api）采集失败，自动 fallback 到模拟数据，
  并把入库数据标记为 data_quality=fallback；
- 每次采集都会记录：数据源、采集时间、成功/失败、数据条数、错误原因。
"""

import logging
from datetime import datetime

from .mock_water_provider import MockWaterProvider
from .water_data_provider import get_provider, ProviderError, WaterDataProvider
from .alert_service import create_alert_if_needed

logger = logging.getLogger("water_collector")

MESSAGES = {
    "mock": "当前使用模拟水情数据",
    "official_api": "当前使用官方实时水情数据",
    "fallback": "真实数据源暂不可用，系统已自动切换至模拟数据",
}


class DataCollector:
    def __init__(self, provider: WaterDataProvider = None):
        self.provider = provider or get_provider()
        self.state = {
            "provider": self.provider.name,
            "display_name": self.provider.display_name,
            "status": "active",
            "message": MESSAGES.get(self.provider.name, "未知数据源"),
            "last_collection_time": "",
            "last_record_count": 0,
            "last_error": "",
        }

    def update_state(self, provider_name: str, display_name: str, status: str, error: str = ""):
        self.state = {
            "provider": provider_name,
            "display_name": display_name,
            "status": status,
            "message": MESSAGES.get("fallback" if status == "fallback" else provider_name, "未知数据源"),
            "last_collection_time": datetime.now().isoformat(timespec="seconds"),
            "last_record_count": 0,
            "last_error": error,
        }

    def collect_all(self) -> dict:
        """执行一次完整采集流程，返回采集报告。"""
        from database import insert_water_data, log_collection

        primary = self.provider
        effective_provider = primary
        error_reason = ""
        fetch_ok = False

        try:
            raw_records = primary.get_current_data()
            fetch_ok = True
        except ProviderError as exc:
            error_reason = f"provider[{primary.name}] 配置错误: {exc}"
        except Exception as exc:  # noqa: BLE001 - 任何异常都走 fallback
            error_reason = f"provider[{primary.name}] 采集失败: {exc}"

        records = []
        if not fetch_ok:
            logger.warning("%s -> fallback to mock", error_reason)
            effective_provider = MockWaterProvider()
            try:
                records = effective_provider.get_current_data()
            except Exception as exc:  # noqa: BLE001
                error_reason = f"fallback 也失败: {exc}"
                self.update_state(primary.name, primary.display_name, "fallback", error_reason)
                self._log_collection_failure(primary.name, error_reason)
                return self.build_report(error=error_reason)
        else:
            records = raw_records or []

        source = effective_provider.name
        data_quality = "valid" if effective_provider.name == primary.name else "fallback"
        status_flag = "active" if effective_provider.name == primary.name else "fallback"

        valid_count = 0
        invalid_count = 0
        persisted = []

        for raw in records:
            from data_normalizer import normalize_record, DataQualityError

            try:
                rec = normalize_record(
                    raw,
                    source=source,
                    data_quality=data_quality,
                    collected_at=datetime.now().isoformat(timespec="seconds"),
                )
            except DataQualityError as exc:
                invalid_count += 1
                logger.warning("数据质量检查未通过: %s (%s)", exc, raw.get("station_id"))
                continue

            insert_water_data(
                station_id=rec["station_id"],
                station_name=rec["station_name"],
                water_level=rec["water_level"],
                warning_level=rec["warning_level"],
                rainfall=rec["rainfall"],
                status=rec["status"],
                source=rec["source"],
                data_quality=rec["data_quality"],
                created_at=rec["timestamp"],
                collected_at=rec["collected_at"],
            )
            valid_count += 1
            persisted.append(rec)

            if rec["status"] != "正常":
                create_alert_if_needed(
                    station_id=rec["station_id"],
                    station_name=rec["station_name"],
                    water_level=rec["water_level"],
                    warning_level=rec["warning_level"],
                    rainfall=rec["rainfall"],
                    status=rec["status"],
                )

        collected_at = datetime.now().isoformat(timespec="seconds")
        log_collection(
            provider=source,
            status="success" if fetch_ok else status_flag,
            record_count=len(records),
            valid_count=valid_count,
            invalid_count=invalid_count,
            fallback_count=valid_count if data_quality == "fallback" else 0,
            error_reason=error_reason,
            collected_at=collected_at,
        )

        logger.info(
            "采集完成 provider=%s records=%d valid=%d invalid=%d error=%s",
            source, len(records), valid_count, invalid_count, error_reason or "无",
        )

        self.state = {
            "provider": source,
            "display_name": effective_provider.display_name,
            "status": status_flag,
            "message": MESSAGES["fallback"] if status_flag == "fallback" else MESSAGES.get(source, "未知数据源"),
            "last_collection_time": collected_at,
            "last_record_count": valid_count,
            "last_error": error_reason,
        }

        return self.build_report(records=persisted, error=error_reason)

    def _log_collection_failure(self, provider: str, error: str):
        from database import log_collection

        collected_at = datetime.now().isoformat(timespec="seconds")
        log_collection(
            provider=provider,
            status="failed",
            record_count=0,
            valid_count=0,
            invalid_count=0,
            fallback_count=0,
            error_reason=error,
            collected_at=collected_at,
        )

    def build_report(self, records=None, error: str = ""):
        return {
            "provider": self.state["provider"],
            "display_name": self.state["display_name"],
            "status": self.state["status"],
            "message": self.state["message"],
            "source": self.state["provider"],
            "records": [
                {
                    "station_id": r["station_id"],
                    "station_name": r["station_name"],
                    "water_level": r["water_level"],
                    "warning_level": r["warning_level"],
                    "rainfall": r["rainfall"],
                    "status": r["status"],
                    "source": r["source"],
                    "data_quality": r["data_quality"],
                    "collected_at": r["collected_at"],
                    "timestamp": r["timestamp"],
                }
                for r in (records or [])
            ],
            "record_count": len(records or []),
            "error": error,
        }

    def get_status(self) -> dict:
        return {
            "provider": self.state["provider"],
            "display_name": self.state["display_name"],
            "status": self.state["status"],
            "message": self.state["message"],
            "source": self.state["provider"],
            "last_collection_time": self.state["last_collection_time"],
            "last_record_count": self.state["last_record_count"],
            "last_error": self.state["last_error"],
        }