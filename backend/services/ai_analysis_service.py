"""AI 智能水情分析服务（Stage 20A 架构设计与最小可运行骨架）。

本阶段定位：
- 只实现 RuleBasedAnalyzer（复用现有水利规则的规则分析），
  analysis_source 固定为 "rule_based"，绝不冒充已接入真实 AI 模型；
- 不调用任何外部 AI API，不引入 API Key / Client Secret，不保存分析结果到数据库；
- 通过 BaseAIAnalyzer 统一接口为后续 Stage 20B（真实 AI 模型接入）预留抽象，
  前端与 main.py 只依赖该接口，不依赖任何具体 AI 厂商。

设计原则：
- 复用现有水情状态算法 calculate_status() 与多站对比分析 build_comparison()，
  不重设计一套与现有预警标准冲突的规则；
- 输入全部来自现有系统数据（water_data / warning_records / 趋势与对比分析），
  不建立第二套水情数据库，不修改现有 water_data 数据表结构。
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from database import (
    get_stations,
    get_all_latest_water_data,
    get_all_history_since,
    count_all_warnings_since,
    get_alerts,
    get_alert_summary,
)
from services.data_analysis import build_comparison
from services.mock_water_provider import calculate_status

# 合法的 AI 风险等级 / 分析来源取值
RISK_LEVELS = ("normal", "attention", "warning", "severe")
ANALYSIS_SOURCES = ("rule_based", "ai_model")

# 现有水情状态 → AI 风险等级 / 基础评分映射（与 calculate_status 保持一致）
STATUS_AI_RISK = {
    "正常": "normal",
    "注意": "attention",
    "警戒": "warning",
    "超警": "severe",
}
RISK_BASE_SCORE = {
    "normal": 15,
    "attention": 45,
    "warning": 72,
    "severe": 95,
}
TREND_CN = {"rising": "上涨", "falling": "下降", "stable": "平稳", "insufficient": "数据不足"}


class AIAnalysisError(Exception):
    """AI 分析器配置或执行错误。"""


def _to_float(value, default=0.0) -> float:
    try:
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return v
    except (TypeError, ValueError):
        return default


# ──────────────────────────── AI 输入模型 ────────────────────────────


@dataclass
class StationSnapshot:
    """单个水文站的当前观测快照（来自现有 water_data / stations 表）。"""

    station_id: str
    station_name: str
    water_level: float
    warning_level: float
    rainfall: float = 0.0
    status: str = "正常"
    source: str = "mock"
    data_quality: str = "valid"
    collected_at: str = ""


@dataclass
class TrendSnapshot:
    """单个水文站的历史趋势摘要。"""

    direction: str = "insufficient"  # rising / falling / stable / insufficient
    slope: float | None = None       # 米/小时
    change: float | None = None      # 米
    recent_values: list = field(default_factory=list)
    window_hours: int = 24


@dataclass
class StatisticSnapshot:
    """单个水文站的统计摘要。"""

    current: float | None = None
    min: float | None = None
    max: float | None = None
    average: float | None = None
    change: float | None = None


@dataclass
class AlertSnapshot:
    """一条未解除（pending / acknowledged）的预警。"""

    id: int
    station_id: str
    station_name: str
    level: str
    title: str
    status: str
    created_at: str


@dataclass
class AIAnalysisContext:
    """AI 分析输入上下文：全部字段来自现有系统数据。"""

    hours: int = 24
    stations: list = field(default_factory=list)          # list[StationSnapshot]
    trends: dict = field(default_factory=dict)            # {station_id: TrendSnapshot}
    statistics: dict = field(default_factory=dict)        # {station_id: StatisticSnapshot}
    alerts: list = field(default_factory=list)            # list[AlertSnapshot]
    alert_summary: dict = field(default_factory=dict)
    risk_ranking: list = field(default_factory=list)


# ──────────────────────────── AI 输出模型 ────────────────────────────


@dataclass
class AIAnalysisOutput:
    """结构化 AI 分析输出。所有字段必填，JSON 可序列化。"""

    risk_level: str
    risk_score: int
    summary: str
    key_findings: list
    trend_analysis: dict
    abnormal_stations: list
    recommendations: list
    generated_at: str
    analysis_source: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "risk_level": self.risk_level,
            "risk_score": int(self.risk_score),
            "summary": self.summary,
            "key_findings": list(self.key_findings),
            "trend_analysis": dict(self.trend_analysis),
            "abnormal_stations": list(self.abnormal_stations),
            "recommendations": list(self.recommendations),
            "generated_at": self.generated_at,
            "analysis_source": self.analysis_source,
            "note": self.note,
        }


# ──────────────────────────── 分析器接口 ────────────────────────────


class BaseAIAnalyzer(ABC):
    """AI 分析器统一接口。

    未来真实分析器（如 OpenAIAnalyzer）只需继承本接口并实现 analyze()，
    前端与 main.py 不需要感知具体厂商。
    """

    analysis_source: str = "rule_based"

    @abstractmethod
    def analyze(self, context: AIAnalysisContext) -> dict:
        raise NotImplementedError


class RuleBasedAnalyzer(BaseAIAnalyzer):
    """规则分析器：模拟未来 AI 分析器接口，实际复用现有水利规则。

    规则：
    1. 站点级风险以现有 calculate_status()（水位/警戒水位/降雨阈值）为唯一事实来源，
       映射为 normal / attention / warning / severe；
    2. 系统整体 risk_level = 最高站点风险；若当前水情全为正常但存在未解除预警，
       则整体风险抬升为 attention（尊重现有预警逻辑）；
    3. risk_score = 最高风险基础分 + 聚合修正（未解除预警数 / 异常站点上涨趋势 /
       暴雨降雨），截断到 0~100；
    4. key_findings / abnormal_stations / recommendations 由上述规则确定性生成。
    """

    analysis_source = "rule_based"

    def analyze(self, context: AIAnalysisContext) -> dict:
        now_iso = datetime.now().isoformat(timespec="seconds")
        hours = int(context.hours or 24)
        stations = context.stations or []

        if not stations:
            return AIAnalysisOutput(
                risk_level="normal",
                risk_score=0,
                summary="当前缺少足够的水情数据，暂无法生成智能分析。",
                key_findings=["暂无足够的站点水情数据"],
                trend_analysis={"overall": "暂无足够数据", "stations": []},
                abnormal_stations=[],
                recommendations=["请稍后重试，待系统采集到有效数据后再生成分析。"],
                generated_at=now_iso,
                analysis_source=self.analysis_source,
            ).to_dict()

        sites = []
        for s in stations:
            wl = _to_float(s.water_level)
            wlv = _to_float(s.warning_level)
            rain = _to_float(s.rainfall)
            try:
                status = calculate_status(wl, wlv, rain)
            except Exception:
                status = (s.status or "正常")
            risk = STATUS_AI_RISK.get(status, "normal")
            ratio = round(wl / wlv, 3) if wlv and wlv > 0 else None
            trend_snap = context.trends.get(s.station_id)
            direction = trend_snap.direction if trend_snap else "insufficient"
            sites.append({
                "station_id": s.station_id,
                "station_name": s.station_name or s.station_id,
                "water_level": round(wl, 2),
                "warning_level": round(wlv, 2),
                "rainfall": round(rain, 1),
                "status": status,
                "risk_level": risk,
                "ratio": ratio,
                "trend": direction,
                "source": s.source or "mock",
                "data_quality": s.data_quality or "valid",
            })

        top = max(sites, key=lambda x: RISK_BASE_SCORE.get(x["risk_level"], 0))
        overall_risk = top["risk_level"]
        alert_count = len(context.alerts or [])
        if overall_risk == "normal" and alert_count > 0:
            overall_risk = "attention"

        score = RISK_BASE_SCORE[overall_risk]
        score += min(10, alert_count * 3)
        if any(x["risk_level"] != "normal" and x["trend"] == "rising" for x in sites):
            score += 5
        if any(x["rainfall"] >= 50 for x in sites):
            score += 5
        score = max(0, min(100, int(round(score))))

        abnormal_stations = [
            {
                "station_id": x["station_id"],
                "station_name": x["station_name"],
                "risk_level": x["risk_level"],
                "status": x["status"],
                "water_level": x["water_level"],
                "warning_level": x["warning_level"],
                "rainfall": x["rainfall"],
            }
            for x in sites
            if x["risk_level"] != "normal"
        ]

        findings = []
        if overall_risk == "severe":
            findings.append(
                f"{top['station_name']}当前水位已达超警级别（{top['water_level']} m），风险极高，建议立即防范。"
            )
        elif overall_risk == "warning":
            findings.append(
                f"{top['station_name']}当前水位已达到警戒级别（{top['water_level']} m / 警戒 {top['warning_level']} m）。"
            )
        elif overall_risk == "attention":
            findings.append(
                f"{top['station_name']}水位接近注意阈值（{top['water_level']} m / 警戒 {top['warning_level']} m），请保持关注。"
            )
        else:
            findings.append("各站点水情均在正常水平，无显著风险。")

        risers = [x for x in sites if x["risk_level"] != "normal" and x["trend"] == "rising"]
        if risers:
            names = "、".join(x["station_name"] for x in risers[:3])
            findings.append(f"水位呈上升趋势：{names}，需关注后续变化。")
        heavy = [x for x in sites if x["rainfall"] >= 15]
        if heavy:
            h = max(heavy, key=lambda x: x["rainfall"])
            findings.append(f"{h['station_name']}降雨较大（{h['rainfall']} mm），需防范短时强降雨。")
        if alert_count:
            findings.append(f"当前存在 {alert_count} 条待处理/已确认预警，请及时处置。")
        if not findings:
            findings.append("无显著异常，各站点水情平稳。")

        trend_stations = []
        for x in sites:
            t = context.trends.get(x["station_id"])
            direction = t.direction if t else "insufficient"
            change = t.change if t else None
            slope = t.slope if t else None
            trend_stations.append({
                "station_id": x["station_id"],
                "station_name": x["station_name"],
                "direction": direction,
                "description": self._trend_desc(direction, change, slope, hours),
            })
        rise_n = sum(1 for x in sites if x["trend"] == "rising")
        fall_n = sum(1 for x in sites if x["trend"] == "falling")
        trend_analysis = {
            "overall": f"过去{hours}小时统计 {len(sites)} 个站点：{rise_n} 个上涨、{fall_n} 个下降，其余平稳。",
            "stations": trend_stations,
        }

        recommendations = []
        if overall_risk == "severe":
            recommendations.append("对超警站点立即启动应急响应，并加密水位及降雨监测频次。")
        if overall_risk == "warning":
            recommendations.append("对达到警戒级别的站点加强监测，提前准备防汛物资。")
        if overall_risk == "attention":
            recommendations.append("对处于注意级别的站点保持持续关注，留意未来水位走向。")
        if risers:
            recommendations.append("持续监测水位上升较快站点的趋势，防范水位快速逼近警戒线。")
        if heavy:
            recommendations.append("降雨量较大地区注意排涝与径流变化，警惕山洪与城市内涝。")
        if alert_count:
            recommendations.append("尽快核实并处置未处理预警，避免风险累积。")
        if not recommendations:
            recommendations.append("维持常规监测频率，继续关注水位与降雨变化。")

        return AIAnalysisOutput(
            risk_level=overall_risk,
            risk_score=score,
            summary=self._summary(overall_risk, len(sites)),
            key_findings=findings,
            trend_analysis=trend_analysis,
            abnormal_stations=abnormal_stations,
            recommendations=recommendations,
            generated_at=now_iso,
            analysis_source=self.analysis_source,
        ).to_dict()

    @staticmethod
    def _trend_desc(direction: str, change, slope, hours: int) -> str:
        if direction == "insufficient":
            return "历史数据不足，暂无法判断趋势"
        cn = TREND_CN.get(direction, "平稳")
        parts = [f"过去{hours}小时水位总体{cn}"]
        if change is not None:
            parts.append(f"变化 {change:+.2f} m")
        if slope is not None:
            parts.append(f"速率约 {abs(slope):.3f} 米/小时")
        return "，".join(parts) + "。"

    @staticmethod
    def _summary(overall_risk: str, station_count: int) -> str:
        if overall_risk == "severe":
            return f"当前有站点水情达到超警级别，整体风险较高，建议立即防范。"
        if overall_risk == "warning":
            return "当前有站点水情达到警戒级别，整体需要重视，请加强监测。"
        if overall_risk == "attention":
            return "当前水情整体处于注意级别，需要保持关注。"
        return "当前各站点水情正常，整体风险较低。"


# ──────────────────────────── 分析器工厂 ────────────────────────────


def get_ai_analyzer(name: str = None) -> BaseAIAnalyzer:
    """根据配置创建分析器实例。

    未指定时读取环境变量 AI_ANALYZER（默认 rule_based）。
    本阶段仅支持规则分析器；真实 AI 模型留待 Stage 20B。
    """
    analyzer_name = (name or os.environ.get("AI_ANALYZER", "rule_based")).strip().lower()
    if analyzer_name in ("rule_based", "rule", "规则"):
        return RuleBasedAnalyzer()
    raise AIAnalysisError(f"未知的 AI 分析器配置: {analyzer_name}")


# ──────────────────────────── 上下文构建与入口 ────────────────────────────


def build_analysis_context(hours: int = 24) -> AIAnalysisContext:
    """从现有系统数据构建 AI 分析上下文。只读，不写数据库。"""
    stations = get_stations()
    latest = get_all_latest_water_data()
    latest_by_id = {r["station_id"]: r for r in latest}
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    all_history = get_all_history_since(since)
    warning_counts = count_all_warnings_since(since)
    comparison = build_comparison(stations, all_history, warning_counts, hours=hours)

    alert_rows = get_alerts(since_iso=since, limit=500)
    alerts = [
        AlertSnapshot(
            id=a["id"],
            station_id=a["station_id"],
            station_name=a["station_name"],
            level=a["warning_level"],
            title=a["title"],
            status=a["status"],
            created_at=a["created_at"],
        )
        for a in alert_rows
        if a["status"] in ("pending", "acknowledged")
    ]
    alert_summary = get_alert_summary()

    comp_by_id = {c["station_id"]: c for c in comparison["stations"]}
    hist_by_station: dict = {}
    for r in all_history:
        hist_by_station.setdefault(r["station_id"], []).append(r)

    snapshots: list = []
    trends: dict = {}
    statistics: dict = {}
    for s in stations:
        sid = s["station_id"]
        rec = latest_by_id.get(sid)
        comp = comp_by_id.get(sid, {})
        snapshots.append(StationSnapshot(
            station_id=sid,
            station_name=s["station_name"],
            water_level=_to_float(rec.get("water_level") if rec else None),
            warning_level=_to_float(s.get("warning_level")),
            rainfall=_to_float(rec.get("rainfall") if rec else None),
            status=(rec.get("status") if rec else "正常") or "正常",
            source=(rec.get("source") if rec else "mock") or "mock",
            data_quality=(rec.get("data_quality") if rec else "valid") or "valid",
            collected_at=(rec.get("created_at") if rec else "") or "",
        ))
        direction = comp.get("water_level_trend") if comp.get("sufficient") else "insufficient"
        trends[sid] = TrendSnapshot(
            direction=direction or "stable",
            slope=comp.get("rate_per_hour"),
            change=comp.get("water_level_change"),
            recent_values=[_to_float(r.get("water_level")) for r in hist_by_station.get(sid, [])][-12:],
            window_hours=hours,
        )
        statistics[sid] = StatisticSnapshot(
            current=comp.get("current_water_level"),
            min=comp.get("min_water_level"),
            max=comp.get("max_water_level"),
            average=comp.get("average_water_level"),
            change=comp.get("water_level_change"),
        )

    return AIAnalysisContext(
        hours=hours,
        stations=snapshots,
        trends=trends,
        statistics=statistics,
        alerts=alerts,
        alert_summary=alert_summary,
        risk_ranking=comparison["risk_ranking"],
    )


def generate_ai_analysis(hours: int = 24) -> dict:
    """读取当前系统水情数据 → 调用分析器 → 返回结构化结果（不写库、不调外部 API）。"""
    context = build_analysis_context(hours=hours)
    analyzer = get_ai_analyzer()
    result = analyzer.analyze(context)
    if isinstance(result, AIAnalysisOutput):
        return result.to_dict()
    return result