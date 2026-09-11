"""历史数据分析与趋势预测（第14阶段）。

使用简单、可解释的统计方法（线性回归 + 移动平均思路）。
不引入任何机器学习模型；预测结果明确标记为“趋势预测”，不作为真实监测数据。
"""

from datetime import datetime

TREND_MIN_POINTS = 3
FORECAST_MIN_POINTS = 3

# 阈值：避免极小的数据波动被误判成明显上升/下降
CHANGE_STABLE_THRESHOLD = 0.10   # 米：区间首尾水位差小于该值视为平稳
RATE_STABLE_THRESHOLD = 0.01     # 米/小时：单位时间变化率小于该值视为平稳

DEFAULT_FORECAST_HORIZON = 6     # 预测未来小时数

INSUFFICIENT_TREND_MSG = "历史数据不足，暂无法进行趋势分析"
INSUFFICIENT_FORECAST_MSG = "历史数据不足，暂无法进行趋势预测"


def _parse_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)[:19])
    except (ValueError, TypeError):
        return None


def _as_points(records: list) -> list:
    """提取 (epoch_seconds, water_level) 有序点序列。"""
    points = []
    for r in records:
        ts = _parse_time(r.get("timestamp") or r.get("created_at"))
        wl = r.get("water_level")
        if ts is None or wl is None:
            continue
        if not isinstance(wl, (int, float)) or isinstance(wl, bool):
            continue
        points.append((ts.timestamp(), float(wl)))
    points.sort(key=lambda p: p[0])
    return points


def _linear_slope_per_hour(points: list) -> float:
    """对时间点序列做线性回归，返回水位变化率（米/小时）。"""
    n = len(points)
    if n < 2:
        return 0.0
    times = [p[0] for p in points]
    levels = [p[1] for p in points]
    mean_t = sum(times) / n
    mean_l = sum(levels) / n
    den = sum((t - mean_t) ** 2 for t in times)
    if den <= 0:
        return 0.0
    num = sum((t - mean_t) * (l - mean_l) for t, l in points)
    slope_per_second = num / den
    return slope_per_second * 3600.0


def _classify_trend(slope_per_hour: float, total_change: float) -> str:
    if abs(total_change) < CHANGE_STABLE_THRESHOLD or abs(slope_per_hour) < RATE_STABLE_THRESHOLD:
        return "stable"
    if slope_per_hour > 0:
        return "rising"
    return "falling"


TREND_DESCRIPTIONS = {
    "rising": "水位总体呈上升趋势",
    "falling": "水位总体呈下降趋势",
    "stable": "水位整体平稳",
}


def analyze_trend(records: list, hours: int = 24) -> dict:
    """计算趋势。数据不足时返回 sufficient=False。"""
    points = _as_points(records)
    if len(points) < TREND_MIN_POINTS:
        return {
            "sufficient": False,
            "message": INSUFFICIENT_TREND_MSG,
            "current_water_level": None,
            "previous_water_level": None,
            "change": None,
            "trend": None,
            "trend_description": INSUFFICIENT_TREND_MSG,
        }

    current = points[-1][1]
    previous = points[0][1]
    total_change = current - previous
    slope_per_hour = _linear_slope_per_hour(points)
    trend = _classify_trend(slope_per_hour, total_change)
    span_hours = max((points[-1][0] - points[0][0]) / 3600.0, 0.001)

    return {
        "sufficient": True,
        "message": "",
        "current_water_level": round(current, 2),
        "previous_water_level": round(previous, 2),
        "change": round(total_change, 2),
        "trend": trend,
        "trend_description": f"过去{hours}小时{TREND_DESCRIPTIONS[trend]}（变化率约 {abs(slope_per_hour):.3f} 米/小时，统计窗口约 {span_hours:.1f} 小时）",
    }


def risk_level_for(water_level: float, warning_level: float) -> str:
    if warning_level <= 0:
        return "normal"
    ratio = water_level / warning_level
    if ratio >= 1.1:
        return "danger"
    if ratio >= 1.0:
        return "warning"
    if ratio >= 0.8:
        return "attention"
    return "normal"


RISK_MESSAGES = {
    "normal": "风险正常，请继续关注水位变化。",
    "attention": "接近注意阈值，请保持关注。",
    "warning": "达到警戒阈值，请加强监测。",
    "danger": "可能超过警戒水位，建议提前防范。",
}


def forecast(records: list, warning_level: float, hours: int = 24,
             horizon_hours: int = DEFAULT_FORECAST_HORIZON) -> dict:
    """基于线性趋势的简单水位预测。数据不足时返回 sufficient=False。"""
    points = _as_points(records)
    if len(points) < FORECAST_MIN_POINTS:
        return {
            "sufficient": False,
            "message": INSUFFICIENT_FORECAST_MSG,
            "is_forecast": True,
            "current_water_level": None,
            "predicted_water_level": None,
            "trend": None,
            "risk_level": None,
            "horizon_hours": horizon_hours,
        }

    current = points[-1][1]
    slope_per_hour = _linear_slope_per_hour(points)
    trend = _classify_trend(slope_per_hour, current - points[0][1])
    predicted = round(max(0.0, current + slope_per_hour * horizon_hours), 2)
    risk = risk_level_for(predicted, warning_level)

    trend_cn = {
        "rising": "上涨",
        "falling": "下降",
        "stable": "平稳",
    }[trend]

    message = (
        f"【趋势预测，仅供参考，非真实监测数据】按近期趋势，预计未来{horizon_hours}小时水位约 "
        f"{predicted} 米（当前 {round(current, 2)} 米，趋势{trend_cn}）。{RISK_MESSAGES[risk]}"
    )

    return {
        "sufficient": True,
        "message": message,
        "is_forecast": True,
        "current_water_level": round(current, 2),
        "predicted_water_level": predicted,
        "trend": trend,
        "risk_level": risk,
        "horizon_hours": horizon_hours,
    }


# ──────────────────────────── 第15阶段：多站综合对比分析 ────────────────────────────

RISK_ORDER = {"danger": 3, "warning": 2, "attention": 1, "normal": 0}


def _station_comparison(station: dict, records: list, warning_count: int, hours: int) -> dict:
    """计算单个站在时间窗口内的对比摘要。数据不足时返回 sufficient=False。"""
    sid = station["station_id"]
    warning_level = station["warning_level"]
    points = _as_points(records)
    n = len(points)

    if n == 0:
        return {
            "station_id": sid,
            "station_name": station["station_name"],
            "warning_level": warning_level,
            "sufficient": False,
            "current_water_level": None,
            "water_level_change": None,
            "water_level_trend": None,
            "rate_per_hour": None,
            "max_water_level": None,
            "min_water_level": None,
            "average_water_level": None,
            "total_rainfall": 0.0,
            "warning_count": warning_count,
            "risk_level": None,
            "risk_ratio": None,
            "data_points": 0,
        }

    levels = [p[1] for p in points]
    current = levels[-1]
    change = current - levels[0]
    slope = _linear_slope_per_hour(points)
    trend = _classify_trend(slope, change)
    risk = risk_level_for(current, warning_level)
    risk_ratio = round(current / warning_level, 3) if warning_level and warning_level > 0 else None
    rainfall_total = round(sum(r.get("rainfall") or 0.0 for r in records), 1)

    return {
        "station_id": sid,
        "station_name": station["station_name"],
        "warning_level": warning_level,
        "sufficient": True,
        "current_water_level": round(current, 2),
        "water_level_change": round(change, 2),
        "water_level_trend": trend,
        "rate_per_hour": round(slope, 4),
        "max_water_level": round(max(levels), 2),
        "min_water_level": round(min(levels), 2),
        "average_water_level": round(sum(levels) / n, 2),
        "total_rainfall": rainfall_total,
        "warning_count": warning_count,
        "risk_level": risk,
        "risk_ratio": risk_ratio,
        "data_points": n,
    }


def _build_series(all_records: list) -> dict:
    """按时间对齐的多站水位序列（用于折线对比图）。"""
    timestamps = sorted({r["created_at"] for r in all_records if r.get("created_at")})
    levels: dict = {}
    for r in all_records:
        levels.setdefault(r["station_id"], {})[r["created_at"]] = r.get("water_level")
    return {
        "timestamps": timestamps,
        "levels": {
            sid: [levels.get(sid, {}).get(ts) for ts in timestamps]
            for sid in (
                sorted({r["station_id"] for r in all_records})
                or sorted(levels.keys())
            )
        },
    }


def build_comparison(stations: list, all_records: list, warning_counts: dict,
                     hours: int = 24) -> dict:
    """构建多站综合对比结果（第15阶段）。

    stations: 全部水文站元数据列表
    all_records: 指定时间窗口内所有站的历史记录
    warning_counts: {station_id: 预警数量}

    返回自足的对比对象；数据不足时对应字段为 None，绝不抛异常。
    """
    by_station: dict = {}
    for r in all_records:
        by_station.setdefault(r["station_id"], []).append(r)

    items = [
        _station_comparison(s, by_station.get(s["station_id"], []),
                            warning_counts.get(s["station_id"], 0), hours)
        for s in stations
    ]

    ranking = sorted(
        items,
        key=lambda x: (RISK_ORDER.get(x["risk_level"] or "normal", 0), x.get("risk_ratio") or 0.0),
        reverse=True,
    )

    def _with_data():
        return [x for x in items if x["sufficient"]]

    def _best(key, reverse=True):
        pool = _with_data()
        if not pool:
            return None
        return max(pool, key=lambda x: x.get(key) or -1e9) if reverse else min(pool, key=lambda x: x.get(key) or 1e9)

    highest_water_level_station = _best("current_water_level")
    highest_rainfall_station = _best("total_rainfall")
    fastest_rising_station = _best("rate_per_hour")
    sufficient_items = _with_data()
    highest_risk_station = (
        max(
            sufficient_items,
            key=lambda x: (RISK_ORDER.get(x["risk_level"] or "normal", -1), x.get("risk_ratio") or 0.0),
        )
        if sufficient_items
        else None
    )

    return {
        "hours": hours,
        "station_count": len(items),
        "sufficient": any(x["sufficient"] for x in items),
        "stations": items,
        "risk_ranking": ranking,
        "highest_water_level_station": highest_water_level_station,
        "fastest_rising_station": fastest_rising_station,
        "highest_rainfall_station": highest_rainfall_station,
        "highest_risk_station": highest_risk_station,
        "series": _build_series(all_records),
    }