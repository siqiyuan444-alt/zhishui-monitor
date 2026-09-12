"""数据标准化与数据质量检查。

把来自不同数据提供器的原始记录统一成一套字段，
并对每条记录做质量检查，异常数据不允许写入数据库。
"""

import math
from datetime import datetime

from .water_data_provider import STATIONS

STATION_ID_SET = {s["station_id"] for s in STATIONS}
STATION_NAME_MAP = {s["station_id"]: s["station_name"] for s in STATIONS}
STATION_LOCATION_MAP = {s["station_id"]: (s["latitude"], s["longitude"]) for s in STATIONS}

SOURCE_MOCK = "mock"
SOURCE_OFFICIAL = "official_api"
SOURCE_MANUAL = "manual"
SOURCE_CHENGDU_OPEN_DATA = "chengdu_open_data"
SOURCE_MOCK_FALLBACK = "mock_fallback"
SOURCE_INVALID_FALLBACK = "invalid_fallback"

QUALITY_VALID = "valid"
QUALITY_INVALID = "invalid"
QUALITY_FALLBACK = "fallback"
QUALITY_GOOD = "good"
QUALITY_DEGRADED = "degraded"

ALLOWED_STATUS = {"正常", "注意", "警戒", "超警"}


class DataQualityError(Exception):
    """数据未通过质量检查。"""


def _parse_timestamp(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def check_record(raw: dict) -> tuple:
    """对单条原始记录做质量校验。

    返回 (is_valid, reason)。reason 为空字符串表示校验通过。
    """
    if not isinstance(raw, dict):
        return False, "记录不是对象"

    station_id = raw.get("station_id")
    if not station_id or str(station_id).strip() not in STATION_ID_SET:
        return False, "station_id 不存在或无效"

    for field in ("water_level", "rainfall"):
        value = raw.get(field, raw.get(field + "", None))
        if value is None:
            return False, f"缺少字段 {field}"
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False, f"{field} 必须是数字"
        if not math.isfinite(value):
            return False, f"{field} 不是有效数字"

    if raw.get("water_level") < 0:
        return False, "水位不能为负数"
    if raw.get("rainfall") < 0:
        return False, "降雨量不能为负数"

    if _parse_timestamp(raw.get("timestamp")) is None:
        return False, "时间戳无效"

    if raw.get("status") and str(raw.get("status")) not in ALLOWED_STATUS:
        return False, "状态值无效"

    return True, ""


def normalize_record(raw: dict, source: str = SOURCE_MOCK, data_quality: str = QUALITY_VALID,
                     collected_at: str = None) -> dict:
    """标准化一条记录，返回统一字段的数据字典。

    未通过质量检查时抛出 DataQualityError。
    """
    is_valid, reason = check_record(raw)
    if not is_valid:
        raise DataQualityError(reason)

    station_id = str(raw["station_id"]).strip()
    timestamp = _parse_timestamp(raw.get("timestamp"))
    lat, lng = STATION_LOCATION_MAP.get(station_id, (None, None))

    return {
        "station_id": station_id,
        "station_name": raw.get("station_name") or STATION_NAME_MAP[station_id],
        "timestamp": timestamp.isoformat(timespec="seconds"),
        "water_level": float(raw["water_level"]),
        "warning_level": float(raw["warning_level"]),
        "rainfall": float(raw["rainfall"]),
        "status": str(raw.get("status") or "正常"),
        "latitude": lat,
        "longitude": lng,
        "source": source,
        "data_quality": data_quality,
        "collected_at": collected_at or datetime.now().isoformat(timespec="seconds"),
        "quality_reason": "",
    }


def safe_normalize(raw: dict, source: str = SOURCE_MOCK, data_quality: str = QUALITY_VALID,
                   collected_at: str = None) -> dict | None:
    """与 normalize_record 相同，但校验失败时返回 None 而不抛异常。"""
    try:
        return normalize_record(raw, source=source, data_quality=data_quality, collected_at=collected_at)
    except DataQualityError:
        return None