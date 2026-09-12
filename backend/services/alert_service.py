"""智能预警服务（第17阶段）。

统一的预警判定与写入入口：

- evaluate_alert(): 基于既有 calculate_status() 判定是否需要预警及预警等级，
  不重新设计已经过测试的水情状态规则；
- create_alert_if_needed(): 幂等写入预警记录——同一站点同一等级若存在未解除
  （pending / acknowledged）的预警则不重复创建；解除（resolved）后再次达到
  触发条件可创建新的预警。

降雨量作为辅助信息一并记录，预警等级以水位状态为主，避免单次极小降雨直接
产生严重预警。
"""

from .mock_water_provider import calculate_status

STATUS_ALERT_LEVEL = {
    "注意": "attention",
    "警戒": "warning",
    "超警": "danger",
}

WARNING_TYPE_MAP = {
    "attention": "注意预警",
    "warning": "警戒预警",
    "danger": "超警预警",
}

ALERT_TITLE = {
    "attention": "{station_name}水位达到注意级别，请加强监测",
    "warning": "{station_name}水位达到警戒级别，请及时关注",
    "danger": "{station_name}水位达到超警级别，请立即防范",
}

ALERT_MESSAGE = {
    "attention": "{station_name}当前水位或降雨量达到注意阈值，请持续关注监测数据。",
    "warning": "{station_name}当前水位或降雨量达到警戒阈值，请及时关注并做好防范准备。",
    "danger": "{station_name}当前水位或降雨量达到超警阈值，请立即采取相应措施。",
}


def evaluate_alert(water_level: float, warning_level: float, rainfall: float) -> dict | None:
    """依据既有状态规则判定预警。水位正常时返回 None，否则返回登记信息。"""
    if warning_level is None or warning_level <= 0:
        return None
    status = calculate_status(water_level, warning_level, rainfall)
    level = STATUS_ALERT_LEVEL.get(status)
    if level is None:
        return None
    return {
        "status": status,
        "level": level,
        "warning_type": WARNING_TYPE_MAP[level],
    }


def create_alert_if_needed(station_id: str, station_name: str, water_level: float,
                           warning_level: float, rainfall: float,
                           status: str = None) -> int | None:
    """幂等创建预警。正常状态不创建；已有未解除的同站点同等级预警时不重复创建。"""
    if status is None:
        status = calculate_status(water_level, warning_level, rainfall)
    level = STATUS_ALERT_LEVEL.get(status)
    if level is None:
        return None

    from database import has_active_alert, create_alert

    if has_active_alert(station_id, level):
        return None

    warning_type = WARNING_TYPE_MAP[level]
    title = ALERT_TITLE[level].format(station_name=station_name)
    message = ALERT_MESSAGE[level].format(station_name=station_name)
    return create_alert(
        station_id=station_id,
        station_name=station_name,
        water_level=water_level,
        warning_level_value=warning_level,
        rainfall=rainfall,
        alert_level=level,
        warning_type=warning_type,
        title=title,
        message=message,
    )