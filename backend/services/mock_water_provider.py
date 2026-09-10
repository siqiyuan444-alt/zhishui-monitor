"""模拟水情数据提供器。

将原本散落在 main.py / database.py 中的模拟数据生成逻辑集中到这里。
在真实数据源接入之前（WATER_DATA_PROVIDER=mock），系统默认使用本提供器。
"""

import random
from datetime import datetime, timedelta

from .water_data_provider import STATIONS, WATER_RANGES, WaterDataProvider


def calculate_status(water_level: float, warning_level: float, rainfall: float) -> str:
    if water_level >= warning_level * 1.1 or rainfall >= 50:
        return "超警"
    if water_level >= warning_level or rainfall >= 30:
        return "警戒"
    if water_level >= warning_level * 0.8 or rainfall >= 15:
        return "注意"
    return "正常"


def get_rainfall_level(rainfall: float) -> str:
    if rainfall >= 50:
        return "暴雨"
    if rainfall >= 30:
        return "大雨"
    if rainfall >= 15:
        return "中雨"
    if rainfall >= 5:
        return "小雨"
    return "无明显降雨"


def generate_rainfall() -> float:
    weights = [
        (0, 5, 50),
        (5, 15, 25),
        (15, 30, 15),
        (30, 50, 7),
        (50, 80, 3),
    ]
    total = sum(w for _, _, w in weights)
    r = random.uniform(0, total)
    cumulative = 0
    for lo, hi, w in weights:
        cumulative += w
        if r <= cumulative:
            return round(random.uniform(lo, hi), 1)
    return 0.0


def build_mock_record(station: dict, timestamp: str = None) -> dict:
    """生成单个水文站的模拟水情记录。"""
    sid = station["station_id"]
    sname = station["station_name"]
    wlevel = station["warning_level"]
    lo, hi = WATER_RANGES[sid]

    if not timestamp:
        timestamp = datetime.now().isoformat(timespec="seconds")

    water_level = round(random.uniform(lo, hi), 2)
    rainfall = generate_rainfall()

    return {
        "station_id": sid,
        "station_name": sname,
        "timestamp": timestamp,
        "water_level": water_level,
        "warning_level": wlevel,
        "rainfall": rainfall,
        "status": calculate_status(water_level, wlevel, rainfall),
        "latitude": station["latitude"],
        "longitude": station["longitude"],
    }


class MockWaterProvider(WaterDataProvider):
    name = "mock"
    display_name = "模拟数据"

    def get_stations(self) -> list:
        return [dict(s) for s in STATIONS]

    def get_current_data(self) -> list:
        now = datetime.now().isoformat(timespec="seconds")
        return [build_mock_record(s, timestamp=now) for s in STATIONS]

    def get_history_data(self, station_id: str, limit: int = 20) -> list:
        station = next((s for s in STATIONS if s["station_id"] == station_id), None)
        if not station:
            return []
        now = datetime.now()
        records = []
        for i in range(limit):
            records.append(
                build_mock_record(
                    station,
                    timestamp=(now - timedelta(minutes=(limit - i) * 5)).isoformat(timespec="seconds"),
                )
            )
        return records