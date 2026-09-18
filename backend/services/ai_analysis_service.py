"""AI 智能水情分析服务（Stage 20A 架构 + Stage 20B 真实 AI 接入）。

架构：
- BaseAIAnalyzer 统一分析器接口，前端与 main.py 只依赖该接口；
- RuleBasedAnalyzer：复用现有水利规则 calculate_status() 的规则分析，
  analysis_source = "rule_based"，作为默认与降级方案；
- LocalWaterAnalyzer：零 API 费用本地智能分析（Stage 20C），
  analysis_source = "local_intelligence"，通过环境变量 AI_ANALYZER=local 启用，
  完全离线运行、绝不调用任何外部 AI / 收费服务；复用系统真实趋势/统计/预测/
  风险/预警结果动态生成中文分析，风险等级严格采纳规则权威结论，绝不降低；
- OpenAIAnalyzer：真实 AI 模型分析（Stage 20B），analysis_source = "ai_model"，
  通过环境变量 AI_ANALYZER=openai 启用，失败时由 generate_ai_analysis() 自动
  降级为 RuleBasedAnalyzer，绝不因 AI 失败导致水情监测系统不可用；
- DoubaoAnalyzer：豆包大模型分析（Stage 20D），通过火山方舟在线推理
  OpenAI 兼容接口调用（Base URL 默认 https://ark.cn-beijing.volces.com/api/v3），
  analysis_source = "ai_model" 且 analysis_provider = "doubao"，
  通过环境变量 AI_ANALYZER=doubao 启用，API Key 仅从 ARK_API_KEY 读取；
  失败时自动降级 LocalWaterAnalyzer，再降级 RuleBasedAnalyzer。

安全与边界：
- API Key / Client Secret 只从环境变量读取，绝不写入代码、日志、异常信息或响应；
- 不请求时（默认 AI_ANALYZER=rule_based）不产生任何外部 AI API 调用与费用；
- AI 只作为“分析解释层”，不得覆盖系统规则：AI 返回的风险等级若低于系统
  calculate_status() 判定，将按系统规则安全修正（不得降低风险）；
- 数据来源为 mock 时必须明确标注为模拟数据，禁止冒充实时官方观测结果；
- AI 返回需经 JSON 解析与 Schema 校验，任何失败自动降级，不向客户端抛 500；
- 复用项目已有 httpx 调用，不引入 OpenAI 官方 SDK 等大型依赖。

设计原则：
- 输入全部来自现有系统数据（water_data / warning_records / 趋势与对比分析），
  不建立第二套水情数据库，不修改现有数据表结构。
"""

import hashlib
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from database import (
    get_stations,
    get_all_latest_water_data,
    get_all_history_since,
    count_all_warnings_since,
    get_alerts,
    get_alert_summary,
)
from services.data_analysis import build_comparison, forecast
from services.mock_water_provider import calculate_status

logger = logging.getLogger("water_monitor.ai_analysis")

# 合法的 AI 风险等级 / 分析来源取值
RISK_LEVELS = ("normal", "attention", "warning", "severe")
ANALYSIS_SOURCES = ("rule_based", "ai_model", "local_intelligence")
RISK_ORDER = {"normal": 0, "attention": 1, "warning": 2, "severe": 3}

# 真实 AI 调用默认配置（环境变量同名覆盖；仅为可选功能，不配置不影响系统运行）
DEFAULT_AI_MODEL = "gpt-4o-mini"
DEFAULT_AI_TIMEOUT = 30
DEFAULT_AI_CACHE_SECONDS = 60

# 豆包大模型（火山方舟在线推理，OpenAI 兼容接口，Stage 20D）
DEFAULT_DOUBAO_MODEL = "doubao-seed-2-0-lite-260215"
DEFAULT_DOUBAO_API_BASE = "https://ark.cn-beijing.volces.com/api/v3"

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
    rainfall_trends: dict = field(default_factory=dict)   # {station_id: {total,direction,recent_values}}
    forecasts: dict = field(default_factory=dict)         # {station_id: forecast_dict}


# ──────────────────────────── AI 输出模型 ────────────────────────────


@dataclass
class AIAnalysisOutput:
    """结构化 AI 分析输出。所有字段必填，JSON 可序列化。

    扩展字段（water_overview / rainfall_analysis / risk_reasons /
    future_outlook / report）仅为 LocalWaterAnalyzer 可选提供，
    空值时不出现在响应中，不影响既有 rule_based / ai_model 输出结构。
    """

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
    model_name: str = ""
    analysis_provider: str = ""
    water_overview: str = ""
    rainfall_analysis: dict = field(default_factory=dict)
    risk_reasons: list = field(default_factory=list)
    future_outlook: str = ""
    report: str = ""

    def to_dict(self) -> dict:
        data = {
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
        if self.model_name:
            data["model_name"] = self.model_name
        if self.analysis_provider:
            data["analysis_provider"] = self.analysis_provider
        if self.water_overview:
            data["water_overview"] = self.water_overview
        if self.rainfall_analysis:
            data["rainfall_analysis"] = dict(self.rainfall_analysis)
        if self.risk_reasons:
            data["risk_reasons"] = list(self.risk_reasons)
        if self.future_outlook:
            data["future_outlook"] = self.future_outlook
        if self.report:
            data["report"] = self.report
        return data


# ──────────────────────────── 分析器接口 ────────────────────────────


class BaseAIAnalyzer(ABC):
    """AI 分析器统一接口。

    未来真实分析器（如 OpenAIAnalyzer）只需继承本接口并实现 analyze()，
    前端与 main.py 不需要感知具体厂商。
    """

    analysis_source: str = "rule_based"
    provider: str = ""

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
    provider = "rule_based"

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
                note=_mock_data_disclaimer(context),
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
            note=_mock_data_disclaimer(context),
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


# ──────────────────────────── 配置与短时缓存 ────────────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _ai_timeout() -> float:
    return float(_env_int("AI_TIMEOUT", DEFAULT_AI_TIMEOUT))


def _cache_seconds() -> int:
    return _env_int("AI_ANALYSIS_CACHE_SECONDS", DEFAULT_AI_CACHE_SECONDS)


_AI_CACHE: dict = {}


def reset_ai_analysis_cache() -> None:
    """清空 AI 分析短时缓存（测试与运维使用）。"""
    _AI_CACHE.clear()


def _cache_key(hours: int, context: "AIAnalysisContext", provider: str = "") -> str:
    """以 provider + hours + 当前各站最新数据状态 + 未解除预警集合作为指纹。"""
    states = sorted(
        f"{s.station_id}:{_to_float(s.water_level):.3f}:{_to_float(s.rainfall):.1f}:{s.collected_at or ''}"
        for s in (context.stations or [])
    )
    alert_ids = sorted(str(a.id) for a in (context.alerts or []))
    fingerprint = hashlib.sha256(
        "|".join(states + [f"alerts:{alert_ids}"]).encode("utf-8")
    ).hexdigest()[:16]
    return f"ai-analysis:{int(hours)}:{provider}:{fingerprint}"


# ──────────────────────────── 通用辅助 ────────────────────────────


def _safe_str(value) -> str:
    return "" if value is None else str(value)


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _mock_data_disclaimer(context: "AIAnalysisContext") -> str:
    """当数据来源包含 mock 时，明确标注模拟数据，禁止冒充真实观测。"""
    if any((getattr(s, "source", "") or "") == "mock" for s in (context.stations or [])):
        return "当前数据来源为模拟数据，不代表真实官方观测结果。"
    return ""


def _append_in_note(data: dict, text: str) -> dict:
    if not text:
        return data
    note = str(data.get("note") or "")
    if text not in note:
        data["note"] = (note + " " + text) if note else text
    return data


# ──────────────────────────── 真实 AI 分析器（OpenAI 兼容） ────────────────────────────


class OpenAIAnalyzer(BaseAIAnalyzer):
    """OpenAI 兼容真实 AI 分析器（Stage 20B）。

    - API Key 与服务端地址只从环境变量读取，绝不写入代码 / 日志 / 响应；
    - 调用失败、超时、JSON 非法或结构校验失败时抛出 AIAnalysisError，
      由 generate_ai_analysis() 统一降级为 RuleBasedAnalyzer；
    - 复用项目已有 httpx，避免为一次调用引入 OpenAI 官方 SDK 等大型依赖。
    """

    analysis_source = "ai_model"
    provider = "openai"

    def __init__(self):
        self.model_name = os.environ.get("AI_MODEL", "").strip() or DEFAULT_AI_MODEL
        self.api_base = (
            os.environ.get("OPENAI_API_BASE", "").strip() or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.timeout = _ai_timeout()

    def analyze(self, context: AIAnalysisContext) -> dict:
        if not self.api_key:
            raise AIAnalysisError("未配置 OPENAI_API_KEY，无法调用真实 AI 模型")
        content = self._call_model(context)
        data = _parse_and_validate(content)
        data = _apply_safety_correction(context, data)
        data = _append_in_note(data, _mock_data_disclaimer(context))
        return AIAnalysisOutput(
            risk_level=data["risk_level"],
            risk_score=data["risk_score"],
            summary=data["summary"],
            key_findings=data["key_findings"],
            trend_analysis=data["trend_analysis"],
            abnormal_stations=data["abnormal_stations"],
            recommendations=data["recommendations"],
            generated_at=data["generated_at"],
            analysis_source="ai_model",
            note=data.get("note", ""),
            model_name=self.model_name,
        ).to_dict()

    def _call_model(self, context: AIAnalysisContext) -> str:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model_name,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _build_system_prompt()},
                {"role": "user", "content": _build_user_prompt(context)},
            ],
        }
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            body = resp.json()
        except httpx.TimeoutException:
            raise AIAnalysisError("OpenAI API 请求超时") from None
        except httpx.HTTPStatusError as exc:
            raise AIAnalysisError(f"OpenAI API 调用失败: HTTP {exc.response.status_code}") from None
        except Exception as exc:
            raise AIAnalysisError(f"OpenAI API 请求异常: {type(exc).__name__}") from None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AIAnalysisError("OpenAI API 响应缺少 choices/message/content") from None
        if not isinstance(content, str) or not content.strip():
            raise AIAnalysisError("OpenAI API 返回内容为空")
        return content


def _build_system_prompt(include_extra: bool = False) -> str:
    base = (
        "你是智慧水利水情分析助手。请仅依据系统传入的水情数据进行分析研判，并严格输出 JSON。\n"
        "硬性要求：\n"
        "1. 不得修改、虚构或遗漏任何输入数据；不得虚构水文站、水位、降雨量或预警信息。\n"
        "2. 不得声称获取了实时官方数据，不得声称访问了成都水务系统或任何外部数据源。\n"
        "3. 若数据来源为 mock/模拟数据，必须明确指出这是模拟数据，不代表真实官方观测结果。\n"
        "4. 数据不足时必须明确说明，不得自行补全数据。\n"
        "5. risk_level 只能取以下枚举值之一：normal、attention、warning、severe。\n"
        "6. risk_score 必须为 0 到 100 之间的整数。\n"
        "7. 输出必须包含以下字段：risk_level, risk_score, summary, key_findings, "
        "trend_analysis, abnormal_stations, recommendations, generated_at, analysis_source, note。\n"
        "   其中 key_findings 与 recommendations 为字符串数组；\n"
        "   abnormal_stations 为对象数组，元素含 station_id, station_name, risk_level, "
        "status, water_level, warning_level, rainfall, trend；\n"
        "   trend_analysis 为对象，包含 overall(字符串) 与 stations(对象数组，"
        "元素含 station_id, station_name, direction, description)。\n"
    )
    if include_extra:
        base += (
            "8. 在满足上述字段的基础上，必须额外输出以下分析字段：\n"
            "   water_overview(总体判断，字符串)；\n"
            "   risk_reasons(风险原因，字符串数组)；\n"
            "   rainfall_analysis(降雨分析，对象，含 overall(字符串) 与 stations(对象数组，"
            "元素含 station_id, station_name, total_rainfall, direction, level))；\n"
            "   future_outlook(未来展望，基于 forecast 预测，字符串)；\n"
            "   report(综合分析报告，将总体判断、关键发现、趋势、风险原因、降雨、未来展望、"
            "建议整合为一段完整中文报告，字符串)。\n"
            "9. 你只负责自然语言分析解释，不得修改或降低系统给出的权威风险等级与风险评分。\n"
            "10. 输出只允许 JSON，不要包含 JSON 之外的解释文字或代码围栏。\n"
        )
    else:
        base += "8. 输出只允许 JSON，不要包含 JSON 之外的解释文字或代码围栏。"
    return base


def _build_user_prompt(context: AIAnalysisContext, include_extra: bool = False) -> str:
    payload = {
        "hours": int(context.hours or 24),
        "stations": [
            {
                "station_id": s.station_id,
                "station_name": s.station_name or s.station_id,
                "water_level": _to_float(s.water_level),
                "warning_level": _to_float(s.warning_level),
                "rainfall": round(_to_float(s.rainfall), 1),
                "status": s.status,
                "source": s.source,
                "data_quality": s.data_quality,
                "collected_at": s.collected_at,
            }
            for s in (context.stations or [])
        ],
        "trends": {
            sid: {
                "direction": t.direction,
                "slope": t.slope,
                "change": t.change,
                "recent_values": (t.recent_values or [])[-12:],
            }
            for sid, t in (context.trends or {}).items()
        },
        "statistics": {
            sid: {
                "current": st.current,
                "min": st.min,
                "max": st.max,
                "average": st.average,
                "change": st.change,
            }
            for sid, st in (context.statistics or {}).items()
        },
        "alerts": [
            {
                "id": a.id,
                "station_name": a.station_name,
                "level": a.level,
                "title": a.title,
                "status": a.status,
                "created_at": a.created_at,
            }
            for a in (context.alerts or [])
        ],
        "alert_summary": context.alert_summary or {},
        "risk_ranking": (context.risk_ranking or [])[:20],
    }
    if include_extra:
        payload["rainfall_trends"] = {
            sid: {
                "total": rt.get("total"),
                "direction": rt.get("direction"),
                "recent_values": (rt.get("recent_values") or [])[-12:],
            }
            for sid, rt in (context.rainfall_trends or {}).items()
        }
        payload["forecasts"] = {
            sid: dict(fn or {}) for sid, fn in (context.forecasts or {}).items()
        }
    return json.dumps(payload, ensure_ascii=False)


def _parse_and_validate(content: str, require_extended: bool = False) -> dict:
    """解析 AI 返回内容并做 Schema 校验；失败抛出 AIAnalysisError。

    require_extended=True 时额外清洗豆包扩展字段（Stage 20D），
    基础字段校验逻辑与 openai 完全一致，不影响既有行为。
    """
    try:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("未找到 JSON 对象")
        raw = json.loads(text[start:end + 1])
    except Exception as exc:
        raise AIAnalysisError(f"AI 返回 JSON 解析失败: {type(exc).__name__}") from None
    if not isinstance(raw, dict):
        raise AIAnalysisError("AI 返回内容不是 JSON 对象")
    result = _validate_output(raw)
    if require_extended:
        _attach_extended_fields(raw, result)
    return result


def _validate_output(raw: dict) -> dict:
    required = (
        "risk_level", "risk_score", "summary", "key_findings",
        "trend_analysis", "abnormal_stations", "recommendations",
        "generated_at", "analysis_source", "note",
    )
    missing = [k for k in required if k not in raw or raw[k] is None]
    if missing:
        raise AIAnalysisError(f"AI 返回缺少必要字段: {','.join(missing)}")

    risk_level = _safe_str(raw["risk_level"]).strip().lower()
    if risk_level not in RISK_LEVELS:
        raise AIAnalysisError(f"AI 返回非法 risk_level: {risk_level}")

    try:
        score = float(raw["risk_score"])
    except (TypeError, ValueError):
        raise AIAnalysisError("AI 返回非法的 risk_score（非数值）") from None
    if not (0 <= score <= 100):
        raise AIAnalysisError(f"AI 返回非法 risk_score（超出 0~100）: {score}")

    summary = _safe_str(raw["summary"]).strip()
    if not summary:
        raise AIAnalysisError("AI 返回 summary 为空")

    if not isinstance(raw["key_findings"], list):
        raise AIAnalysisError("AI 返回 key_findings 不是数组")
    if not isinstance(raw["trend_analysis"], dict):
        raise AIAnalysisError("AI 返回 trend_analysis 不是对象")
    if not isinstance(raw["abnormal_stations"], list):
        raise AIAnalysisError("AI 返回 abnormal_stations 不是数组")
    if not isinstance(raw["recommendations"], list):
        raise AIAnalysisError("AI 返回 recommendations 不是数组")

    trend = raw["trend_analysis"]
    trend_stations = trend.get("stations")
    overall = _safe_str(trend.get("overall")).strip()
    if not isinstance(trend_stations, list):
        raise AIAnalysisError("AI 返回 trend_analysis.stations 不是数组")
    if not overall:
        raise AIAnalysisError("AI 返回 trend_analysis.overall 为空")

    return {
        "risk_level": risk_level,
        "risk_score": int(round(score)),
        "summary": _truncate(summary, 2000),
        "key_findings": [_truncate(_safe_str(x), 500) for x in raw["key_findings"]][:20],
        "trend_analysis": {
            "overall": _truncate(overall, 1000),
            "stations": [_clean_trend_station(x) for x in trend_stations if isinstance(x, dict)][:50],
        },
        "abnormal_stations": [
            _clean_abnormal_station(x) for x in raw["abnormal_stations"] if isinstance(x, dict)
        ][:50],
        "recommendations": [_truncate(_safe_str(x), 500) for x in raw["recommendations"]][:20],
        "generated_at": _safe_str(raw["generated_at"]) or datetime.now().isoformat(timespec="seconds"),
        "note": _truncate(_safe_str(raw.get("note", "")), 500),
    }


def _clean_abnormal_station(x: dict) -> dict:
    rl = _safe_str(x.get("risk_level")).strip().lower()
    if rl not in RISK_LEVELS:
        rl = "attention"
    return {
        "station_id": _safe_str(x.get("station_id")),
        "station_name": _safe_str(x.get("station_name")) or _safe_str(x.get("station_id")),
        "risk_level": rl,
        "status": _safe_str(x.get("status")),
        "water_level": _to_float(x.get("water_level")),
        "warning_level": _to_float(x.get("warning_level")),
        "rainfall": round(_to_float(x.get("rainfall")), 1),
    }


def _clean_trend_station(x: dict) -> dict:
    return {
        "station_id": _safe_str(x.get("station_id")),
        "station_name": _safe_str(x.get("station_name")) or _safe_str(x.get("station_id")),
        "direction": _safe_str(x.get("direction")) or "stable",
        "description": _truncate(_safe_str(x.get("description")) or "暂无描述", 300),
    }


def _clean_rain_station(x: dict) -> dict:
    return {
        "station_id": _safe_str(x.get("station_id")),
        "station_name": _safe_str(x.get("station_name")) or _safe_str(x.get("station_id")),
        "total_rainfall": round(_to_float(x.get("total_rainfall")), 1),
        "direction": _safe_str(x.get("direction")) or "insufficient",
        "level": _truncate(_safe_str(x.get("level")), 50),
    }


def _clean_rainfall_analysis(value) -> dict:
    """清洗可选降雨分析字段；结构非法或为空时返回空 dict（不进入响应）。"""
    if not isinstance(value, dict):
        return {}
    stations = value.get("stations")
    cleaned = {
        "overall": _truncate(_safe_str(value.get("overall")), 1000),
        "stations": [_clean_rain_station(x) for x in stations if isinstance(x, dict)][:50]
        if isinstance(stations, list)
        else [],
    }
    if cleaned["overall"] or cleaned["stations"]:
        return cleaned
    return {}


def _attach_extended_fields(raw: dict, result: dict) -> None:
    """为豆包输出附加可选扩展字段（Stage 20D）。缺失字段不产生键，向后兼容。"""
    water_overview = _truncate(_safe_str(raw.get("water_overview")), 2000)
    if water_overview:
        result["water_overview"] = water_overview

    rainfall = _clean_rainfall_analysis(raw.get("rainfall_analysis"))
    if rainfall:
        result["rainfall_analysis"] = rainfall

    reasons = [
        _truncate(_safe_str(x), 500)
        for x in raw.get("risk_reasons", [])
        if isinstance(x, (str, int, float)) and _safe_str(x).strip()
    ][:8]
    if reasons:
        result["risk_reasons"] = reasons

    future = _truncate(_safe_str(raw.get("future_outlook")), 1000)
    if future:
        result["future_outlook"] = future

    report = _truncate(_safe_str(raw.get("report")), 4000)
    if report:
        result["report"] = report


def _apply_safety_correction(context: AIAnalysisContext, data: dict) -> dict:
    """AI 是分析解释层，不是新的安全规则引擎。

    当 AI 返回的风险低于系统 calculate_status() 判定时，以系统规则为准升级，
    绝不允许 AI 降低系统已判定的风险等级。
    """
    rule = RuleBasedAnalyzer().analyze(context)
    rule_risk = rule["risk_level"]
    data_risk = data["risk_level"]
    if RISK_ORDER[data_risk] < RISK_ORDER[rule_risk]:
        data["risk_level"] = rule_risk
        data["risk_score"] = max(int(data["risk_score"]), int(rule["risk_score"]))
        seg = "已按系统安全规则校准风险等级。"
        data["note"] = seg if not data.get("note") else f"{data['note']} {seg}"
    known = {s.get("station_id") for s in data["abnormal_stations"]}
    for x in rule["abnormal_stations"]:
        sid = x.get("station_id")
        if sid and sid not in known:
            data["abnormal_stations"].append(x)
            known.add(sid)
    if not data["recommendations"]:
        data["recommendations"] = list(rule["recommendations"])
    if not data["key_findings"]:
        data["key_findings"] = list(rule["key_findings"])
    return data


# ──────────────────────────── 豆包大模型分析器（Stage 20D） ────────────────────────────


class DoubaoAnalyzer(BaseAIAnalyzer):
    """豆包大模型分析器（火山方舟在线推理，OpenAI 兼容接口）。

    - Base URL 默认 https://ark.cn-beijing.volces.com/api/v3（在线推理，非 Coding Plan）；
    - API Key 只从环境变量 ARK_API_KEY 读取，绝不写入代码 / 日志 / 响应；
    - 模型 ID 通过 DOUBAO_MODEL 配置，默认 doubao-seed-2-0-lite-260215；
    - 复用项目已有 httpx 与 OpenAI 兼容 /chat/completions 协议，不新增依赖；
    - analysis_source = "ai_model" 且 analysis_provider = "doubao"，
      自动复用现有 AI 缓存、限流与超时机制；
    - 任何失败（缺 key、超时、网络、HTTP 4xx/429/5xx、格式/结构错误、空内容）
      均抛出 AIAnalysisError，由 generate_ai_analysis() 降级为 LocalWaterAnalyzer，
      再降级 RuleBasedAnalyzer，绝不向客户端抛 500；
    - 风险等级受 _apply_safety_correction 约束，只能与系统权威风险一致或更高，
      绝不允许降低。
    """

    analysis_source = "ai_model"
    provider = "doubao"

    def __init__(self):
        self.model_name = (
            os.environ.get("DOUBAO_MODEL", "").strip() or DEFAULT_DOUBAO_MODEL
        )
        self.api_base = (
            os.environ.get("DOUBAO_API_BASE_URL", "").strip() or DEFAULT_DOUBAO_API_BASE
        ).rstrip("/")
        self.api_key = os.environ.get("ARK_API_KEY", "").strip()
        self.timeout = _ai_timeout()

    def analyze(self, context: AIAnalysisContext) -> dict:
        if not self.api_key:
            raise AIAnalysisError("未配置 ARK_API_KEY，无法调用豆包大模型")
        content = self._call_model(context)
        data = _parse_and_validate(content, require_extended=True)
        data = _apply_safety_correction(context, data)
        data = _append_in_note(data, _mock_data_disclaimer(context))
        return AIAnalysisOutput(
            risk_level=data["risk_level"],
            risk_score=data["risk_score"],
            summary=data["summary"],
            key_findings=data["key_findings"],
            trend_analysis=data["trend_analysis"],
            abnormal_stations=data["abnormal_stations"],
            recommendations=data["recommendations"],
            generated_at=data["generated_at"],
            analysis_source="ai_model",
            note=data.get("note", ""),
            model_name=self.model_name,
            analysis_provider="doubao",
            water_overview=data.get("water_overview", ""),
            rainfall_analysis=data.get("rainfall_analysis", {}),
            risk_reasons=data.get("risk_reasons", []),
            future_outlook=data.get("future_outlook", ""),
            report=data.get("report", ""),
        ).to_dict()

    def _call_model(self, context: AIAnalysisContext) -> str:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model_name,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _build_system_prompt(include_extra=True)},
                {"role": "user", "content": _build_user_prompt(context, include_extra=True)},
            ],
        }
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            body = resp.json()
        except httpx.TimeoutException:
            raise AIAnalysisError("豆包 API 请求超时") from None
        except httpx.HTTPStatusError as exc:
            raise AIAnalysisError(
                f"豆包 API 调用失败: HTTP {exc.response.status_code}"
            ) from None
        except Exception as exc:
            raise AIAnalysisError(f"豆包 API 请求异常: {type(exc).__name__}") from None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AIAnalysisError("豆包 API 响应缺少 choices/message/content") from None
        if not isinstance(content, str) or not content.strip():
            raise AIAnalysisError("豆包 API 返回内容为空")
        return content


# ──────────────────────────── LocalWaterAnalyzer（零费用本地智能分析） ────────────────────────────

# 本地分析固定的预测展望窗口小时数（与系统 forecast 默认一致）
LOCAL_HORIZON_HOURS = 6
# 降雨强度档阈值（mm，统计窗口合计）
RAIN_STRONG_THRESHOLD = 15.0
RAIN_HEAVY_THRESHOLD = 50.0

LOCAL_RISK_CN = {"normal": "正常", "attention": "注意", "warning": "警戒", "severe": "超警"}
LOCAL_TREND_CN = {"rising": "上涨", "falling": "下降", "stable": "平稳", "insufficient": "数据不足"}


def _pick_variant(variants: list, seed) -> str:
    """从语义等价的句子池中按数据派生种子选取一句（确定性、非随机）。"""
    if not variants:
        return ""
    return variants[abs(int(seed)) % len(variants)]


def _rain_level_text(total: float) -> str:
    if total < 0.5:
        return "基本无降雨"
    if total < 5:
        return "小雨"
    if total < 15:
        return "中雨"
    if total < 50:
        return "较强降雨"
    return "强降雨"


def _rain_direction_text(direction: str) -> str:
    return {
        "rising": "降雨在增强",
        "falling": "降雨在减弱",
        "stable": "降雨总体平稳",
        "none": "基本无降雨",
        "insufficient": "降雨数据不足",
    }.get(direction, "降雨数据不足")


def _rainfall_trend_for(records: list) -> dict:
    """按时间序列切前/后两段比较均值，判定降雨强弱趋势（纯阈值、确定性）。"""
    rains = [max(0.0, _to_float(r.get("rainfall"))) for r in (records or [])]
    total = round(sum(rains), 1)
    direction = "insufficient"
    if len(rains) < 3:
        direction = "insufficient" if total > 0 else "none"
    elif total <= 0.0:
        direction = "none"
    else:
        half = len(rains) // 2
        first = rains[:half]
        second = rains[half:]
        f_mean = sum(first) / len(first)
        s_mean = sum(second) / len(second)
        if s_mean >= f_mean * 1.2 and s_mean >= 0.2:
            direction = "rising"
        elif f_mean >= s_mean * 1.2 and f_mean >= 0.2:
            direction = "falling"
        else:
            direction = "stable"
    return {"total": total, "direction": direction, "recent_values": rains[-12:]}


class LocalWaterAnalyzer(BaseAIAnalyzer):
    """零 API 费用的本地智能水情分析器（Stage 20C）。

    - 完全不调用 OpenAI 或任何收费云端 AI，默认离线可用；
    - 输入全部来自系统已有真实计算结果：当前水位、警戒水位、水位差与比值、
      变化斜率、降雨量与降雨趋势、历史统计、system 风险等级、forecast 预测、
      未解除预警；绝不随机生成、绝不冒充外部大模型；
    - risk_level / risk_score 直接采纳 RuleBasedAnalyzer 的权威聚合结论
      （系统 calculate_status），本地分析只撰写更丰富的动态中文解释，
      结构上保证不会降低权威风险等级；
    - 文本由“事实单元 + 确定性变体池”组合生成：同一数据恒产出同一文案，
      数据变化文案随之变化，不机械重复；
    - 任何内部异常由 generate_ai_analysis() 统一降级为 RuleBasedAnalyzer。
    """

    analysis_source = "local_intelligence"
    provider = "local_intelligence"

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
                note=_mock_data_disclaimer(context),
            ).to_dict()

        # 权威风险结论：与系统规则完全一致，本地分析绝不降低
        authoritative = RuleBasedAnalyzer().analyze(context)
        risk_level = authoritative["risk_level"]
        risk_score = int(authoritative["risk_score"])

        sites = self._build_sites(context)

        overview = self._overview(sites)
        water_trends = self._water_trends(context, sites, hours)
        rain_analysis = self._rain_analysis(sites)
        key_findings = self._key_findings(context, sites, authoritative, rain_analysis)
        reasons = self._risk_reasons(context, sites, authoritative)
        future = self._future_outlook(sites)
        recommendations = self._recommendations(context, sites, authoritative, rain_analysis)
        report = self._report(sites, rain_analysis, authoritative, future, hours)

        return AIAnalysisOutput(
            risk_level=risk_level,
            risk_score=risk_score,
            summary=overview,
            water_overview=overview,
            key_findings=key_findings,
            trend_analysis={
                "overall": water_trends["overall"],
                "stations": water_trends["stations"],
            },
            abnormal_stations=authoritative["abnormal_stations"],
            recommendations=recommendations,
            rainfall_analysis=rain_analysis,
            risk_reasons=reasons,
            future_outlook=future,
            report=report,
            generated_at=now_iso,
            analysis_source=self.analysis_source,
            note=_mock_data_disclaimer(context),
        ).to_dict()

    @staticmethod
    def _build_sites(context: AIAnalysisContext) -> list:
        sites = []
        for s in (context.stations or []):
            wl = _to_float(s.water_level)
            wlv = _to_float(s.warning_level)
            rain = _to_float(s.rainfall)
            try:
                status = calculate_status(wl, wlv, rain)
            except Exception:
                status = (s.status or "正常")
            risk = STATUS_AI_RISK.get(status, "normal")
            gap = round(wlv - wl, 2) if wlv > 0 else None
            ratio = round(wl / wlv, 3) if wlv > 0 else None
            t = context.trends.get(s.station_id) or TrendSnapshot()
            rt = (context.rainfall_trends or {}).get(s.station_id) or {
                "total": round(rain, 1), "direction": "insufficient", "recent_values": [],
            }
            sites.append({
                "station_id": s.station_id,
                "station_name": s.station_name or s.station_id,
                "water_level": wl,
                "warning_level": wlv,
                "gap": gap,
                "ratio": ratio,
                "rainfall": round(rain, 1),
                "rain_total": float(rt.get("total") or 0.0),
                "rain_direction": rt.get("direction", "insufficient"),
                "status": status,
                "risk_level": risk,
                "trend": (t.direction if t else "insufficient"),
                "change": (t.change if t else None),
                "slope": (t.slope if t else None),
                "forecast": (context.forecasts or {}).get(s.station_id) or {},
                "source": s.source or "mock",
                "data_quality": s.data_quality or "valid",
            })
        return sites

    @staticmethod
    def _overview(sites: list) -> str:
        counts = {"正常": 0, "注意": 0, "警戒": 0, "超警": 0}
        for x in sites:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        top = max(sites, key=lambda x: RISK_BASE_SCORE.get(x["risk_level"], 0))
        seg = (
            f"共监测 {len(sites)} 个站点：正常 {counts['正常']}、注意 {counts['注意']}、"
            f"警戒 {counts['警戒']}、超警 {counts['超警']}。"
        )
        if top["risk_level"] != "normal":
            seg += (
                f"当前风险最高为{top['station_name']}，水位 {top['water_level']:.2f} m"
                f"（警戒 {top['warning_level']:.2f} m），处于{LOCAL_RISK_CN.get(top['risk_level'], '注意')}状态。"
            )
        else:
            seg += f"当前各站点水位均在正常范围，最高水位 {top['water_level']:.2f} m（{top['station_name']}）。"
        return seg

    @staticmethod
    def _water_trends(context: AIAnalysisContext, sites: list, hours: int) -> dict:
        rise = sum(1 for x in sites if x["trend"] == "rising")
        fall = sum(1 for x in sites if x["trend"] == "falling")
        overall = f"过去{hours}小时统计 {len(sites)} 个站点：{rise} 个上涨、{fall} 个下降，其余平稳。"
        ordered = sorted(sites, key=lambda x: -(abs(x["slope"]) if x["slope"] is not None else 0))
        stations = [
            {
                "station_id": x["station_id"],
                "station_name": x["station_name"],
                "direction": x["trend"],
                "description": RuleBasedAnalyzer._trend_desc(x["trend"], x["change"], x["slope"], hours),
            }
            for x in ordered
        ]
        return {"overall": overall, "stations": stations}

    @classmethod
    def _rain_analysis(cls, sites: list) -> dict:
        items = [
            {
                "station_id": x["station_id"],
                "station_name": x["station_name"],
                "total_rainfall": round(x["rain_total"], 1),
                "direction": x["rain_direction"],
                "level": _rain_level_text(x["rain_total"]),
            }
            for x in sites
        ]
        total_all = round(sum(i["total_rainfall"] for i in items), 1)
        if total_all <= 0:
            overall = "统计窗口内各站点基本无降雨。"
        else:
            hi = max(items, key=lambda i: i["total_rainfall"])
            overall = (
                f"统计窗口内各站降雨合计 {total_all} mm，其中{hi['station_name']}最多"
                f"（{hi['total_rainfall']:.1f} mm，{hi['level']}），{_rain_direction_text(hi['direction'])}。"
            )
        return {"overall": overall, "stations": items, "total_all": total_all}

    def _key_findings(self, context, sites, authoritative, rain_analysis) -> list:
        out = list(authoritative.get("key_findings") or [])
        if rain_analysis.get("total_all", 0) >= RAIN_STRONG_THRESHOLD:
            hi = max(
                (i for i in rain_analysis.get("stations", [])),
                key=lambda i: i["total_rainfall"],
                default=None,
            )
            if hi:
                out.append(
                    f"{hi['station_name']}统计窗口降雨合计 {hi['total_rainfall']:.1f} mm（{hi['level']}），"
                    f"{_rain_direction_text(hi['direction'])}。"
                )
        odd = [
            (x, fn) for x in sites
            for fn in [x.get("forecast") or {}]
            if fn.get("sufficient") and fn.get("risk_level") in ("warning", "danger")
            and x["risk_level"] != "severe"
        ]
        seen = set()
        merged = []
        for item in out:
            if item not in seen:
                seen.add(item)
                merged.append(item)
        for x, fn in odd[:2]:
            text = f"{x['station_name']}若沿当前趋势发展，未来{LOCAL_HORIZON_HOURS}小时可能达到更高风险档。"
            if text not in seen and len(merged) < 6:
                seen.add(text)
                merged.append(text)
        if not merged:
            merged.append("无显著异常，各站点水情平稳。")
        return merged[:6]

    def _risk_reasons(self, context, sites, authoritative) -> list:
        reasons = []
        by_id = {x["station_id"]: x for x in sites}
        for a in (authoritative.get("abnormal_stations") or []):
            x = by_id.get(a.get("station_id"))
            if not x:
                continue
            ratio = x["ratio"] or 0.0
            if a.get("risk_level") == "severe" or ratio >= 1.0:
                reasons.append(
                    f"{x['station_name']}水位 {x['water_level']:.2f} m 已达/超过警戒 "
                    f"{x['warning_level']:.2f} m。"
                )
            elif a.get("risk_level") == "warning" or ratio >= 0.8:
                reasons.append(
                    f"{x['station_name']}水位与警戒水位比值达 {ratio * 100:.0f}%，接近/达到警戒阈值。"
                )
            else:
                reasons.append(
                    f"{x['station_name']}水情处于注意状态（水位 {x['water_level']:.2f} m / "
                    f"警戒 {x['warning_level']:.2f} m）。"
                )
        risers = [x for x in sites if x["trend"] == "rising"]
        if risers:
            fastest = max((x["slope"] or 0.0 for x in risers), default=0.0)
            names = "、".join(x["station_name"] for x in sorted(risers, key=lambda i: -(i["slope"] or 0))[:3])
            reasons.append(f"{names}水位呈上升趋势（最快约 {abs(fastest):.3f} 米/小时）。")
        heavy = [x for x in sites if x["rain_total"] >= RAIN_HEAVY_THRESHOLD]
        if heavy:
            names = "、".join(x["station_name"] for x in heavy[:3])
            reasons.append(f"{names}统计窗口降雨量大（≥50 mm），需防范积水与径流风险。")
        elif [x for x in sites if x["rain_total"] >= RAIN_STRONG_THRESHOLD]:
            names = "、".join(
                x["station_name"] for x in sites
                if RAIN_STRONG_THRESHOLD <= x["rain_total"] < RAIN_HEAVY_THRESHOLD
            )
            reasons.append(f"{names}统计窗口为较强降雨，注意雨水汇聚影响。")
        alert_count = len(context.alerts or [])
        if alert_count:
            reasons.append(f"当前存在 {alert_count} 条待处理/已确认预警，风险尚未解除。")
        if not reasons:
            reasons.append("当前各项指标均在正常范围，无显著风险诱因。")
        return reasons[:8]

    @classmethod
    def _future_outlook(cls, sites: list) -> str:
        parts = []
        for x in sites:
            fn = x.get("forecast") or {}
            if not fn.get("sufficient") or fn.get("predicted_water_level") is None:
                continue
            pred = float(fn["predicted_water_level"])
            trend_cn = LOCAL_TREND_CN.get(fn.get("trend"), "平稳")
            seg = (
                f"按近期趋势，预计未来{LOCAL_HORIZON_HOURS}小时{x['station_name']}水位约 {pred:.2f} m"
                f"（当前 {x['water_level']:.2f} m，趋势{trend_cn}）"
            )
            if fn.get("risk_level") in ("warning", "danger"):
                seg += "，或达到更高风险警戒档，需重点防范"
            elif fn.get("risk_level") == "attention":
                seg += "，已进入需要关注的区间"
            seg += "。"
            parts.append(seg)
        if not parts:
            return "历史数据不足，暂无法提供未来趋势预判。"
        return " ".join(parts[:2]) + "（趋势预测，仅供参考，非真实监测数据）"

    def _recommendations(self, context, sites, authoritative, rain_analysis) -> list:
        risk = authoritative["risk_level"]
        recs = []
        if risk == "severe":
            recs.append("对超警站点立即启动应急响应，并加密水位及降雨监测频次。")
        if risk == "warning":
            recs.append("对达到警戒级别的站点加强监测，提前准备防汛物资。")
        if risk == "attention":
            recs.append("对处于注意级别的站点保持持续关注，留意未来水位走向。")
        risers = [x for x in sites if x["trend"] == "rising"]
        if risers:
            names = "、".join(x["station_name"] for x in risers[:3])
            recs.append(f"持续观察 {names} 的水位上升趋势，关注未来{LOCAL_HORIZON_HOURS}小时是否逼近警戒线。")
        strong = [x for x in sites if x["rain_total"] >= RAIN_HEAVY_THRESHOLD]
        if strong:
            recs.append("强降雨站点注意排涝与径流变化，警惕内涝与山洪风险。")
        elif rain_analysis.get("total_all", 0) >= RAIN_STRONG_THRESHOLD:
            recs.append("较强降雨条件下注意雨水汇集对水位的影响。")
        if len(context.alerts or []):
            recs.append("尽快核实并处置未处理预警，避免风险累积。")
        if not recs:
            recs.append("维持常规监测频率，继续关注水位与降雨变化。")
        return recs[:8]

    def _report(self, sites, rain_analysis, authoritative, future, hours) -> str:
        risk = authoritative["risk_level"]
        top = max(sites, key=lambda x: RISK_BASE_SCORE.get(x["risk_level"], 0))
        trend_cn = LOCAL_TREND_CN.get(top["trend"], "平稳")
        seed = int(
            abs(top["water_level"] * 100)
            + abs((top["slope"] or 0.0) * 1000)
            + top["rain_total"] * 10
        )
        parts = []
        if top["risk_level"] != "normal":
            opener = _pick_variant([
                f"过去{hours}小时，{top['station_name']}水位{trend_cn}",
                f"近期，{top['station_name']}水位{trend_cn}",
                f"过去{hours}小时观测到{top['station_name']}水位{trend_cn}",
            ], seed)
            parts.append(
                f"{opener}，当前水位 {top['water_level']:.2f} m，警戒水位 {top['warning_level']:.2f} m，"
                f"处于{LOCAL_RISK_CN.get(top['risk_level'], '注意')}状态。"
            )
            if top["gap"] is not None:
                if top["gap"] >= 0:
                    relevant = _pick_variant([
                        f"目前距警戒水位仅 {top['gap']:.2f} m",
                        f"当前距离警戒线 {top['gap']:.2f} m",
                    ], seed)
                    parts.append(f"{relevant}，需要密切关注。")
                else:
                    parts.append(f"当前已超警戒水位 {abs(top['gap']):.2f} m，形势严峻。")
        else:
            if top["gap"] is not None:
                parts.append(
                    f"过去{hours}小时，{top['station_name']}等站点水位总体{trend_cn}，"
                    f"当前低于警戒水位 {top['gap']:.2f} m。"
                )
            else:
                parts.append(f"过去{hours}小时，{top['station_name']}等站点水位总体{trend_cn}。")
        if rain_analysis.get("total_all", 0) >= RAIN_STRONG_THRESHOLD:
            hi = max(rain_analysis.get("stations", []), key=lambda i: i["total_rainfall"], default=None)
            if hi:
                parts.append(
                    f"同期各站降雨合计 {rain_analysis['total_all']:.1f} mm，"
                    f"其中{hi['station_name']}为{hi['level']}，{_rain_direction_text(hi['direction'])}。"
                )
        parts.append(f"综合系统风险判定，当前整体处于{LOCAL_RISK_CN.get(risk, '正常')}级别（评分 {authoritative['risk_score']}/100）。")
        abnormal = (authoritative.get("abnormal_stations") or [])
        if abnormal:
            names = "、".join(x.get("station_name") for x in abnormal[:3])
            parts.append(f"需重点关注站点：{names}。")
        if future and future != "历史数据不足，暂无法提供未来趋势预判。":
            parts.append(future.rstrip("。") + "。")
        parts.append("建议持续关注未来1～3小时的水位与降雨变化，并根据风险等级及时响应。")
        return " ".join(parts)


# ──────────────────────────── 分析器工厂 ────────────────────────────


def get_ai_analyzer(name: str = None) -> BaseAIAnalyzer:
    """根据配置创建分析器实例。

    未指定时读取环境变量 AI_ANALYZER（默认 rule_based）。
    - rule_based：默认规则分析，不调用任何外部 API；
    - local：LocalWaterAnalyzer 本地智能分析，零 API 费用、完全离线，
      失败自动降级 rule_based；
    - doubao：DoubaoAnalyzer 豆包大模型（火山方舟在线推理，需配置 ARK_API_KEY，
      失败自动降级 local → rule_based）；
    - openai：OpenAI 兼容真实 AI 分析器（需配置 OPENAI_API_KEY，失败自动降级）。
    """
    analyzer_name = (name or os.environ.get("AI_ANALYZER", "rule_based")).strip().lower()
    if analyzer_name in ("rule_based", "rule", "规则"):
        return RuleBasedAnalyzer()
    if analyzer_name in ("local", "local_intelligence", "本地", "local_ai"):
        return LocalWaterAnalyzer()
    if analyzer_name in ("doubao", "doubao_analyzer", "豆包", "ark", "volcano"):
        return DoubaoAnalyzer()
    if analyzer_name in ("openai", "gpt", "chatgpt", "ai_model"):
        return OpenAIAnalyzer()
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
    rainfall_trends: dict = {}
    forecasts: dict = {}
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
        rainfall_trends[sid] = _rainfall_trend_for(hist_by_station.get(sid, []))
        forecasts[sid] = forecast(
            hist_by_station.get(sid, []),
            warning_level=_to_float(s.get("warning_level")),
            hours=hours,
            horizon_hours=LOCAL_HORIZON_HOURS,
        )

    return AIAnalysisContext(
        hours=hours,
        stations=snapshots,
        trends=trends,
        statistics=statistics,
        alerts=alerts,
        alert_summary=alert_summary,
        risk_ranking=comparison["risk_ranking"],
        rainfall_trends=rainfall_trends,
        forecasts=forecasts,
    )


def generate_ai_analysis(hours: int = 24) -> dict:
    """读取当前系统水情数据 → 调用所选分析器 → 返回结构化结果（不写库）。

    - 默认 rule_based，不产生任何外部 AI API 调用；
    - AI_ANALYZER=local 时使用 LocalWaterAnalyzer 本地智能分析（离线、零费用），
      任何失败自动降级 RuleBasedAnalyzer；
    - AI_ANALYZER=doubao 时调用豆包大模型；任何失败自动降级 LocalWaterAnalyzer，
      再降级 RuleBasedAnalyzer，并在 note 中说明降级原因；
    - AI_ANALYZER=openai 时调用真实模型；任何失败自动降级 RuleBasedAnalyzer，
      并在 note 中说明“真实 AI 分析暂不可用，当前使用规则分析结果”；
    - 仅对真实模型成功结果做短时缓存（AI_ANALYSIS_CACHE_SECONDS 秒，默认 60），
      相同 provider + hours + 数据状态短时间内不重复调用模型。
    """
    context = build_analysis_context(hours=hours)
    analyzer = get_ai_analyzer()
    is_ai = analyzer.analysis_source == "ai_model"
    cache_seconds = _cache_seconds()
    key = (
        _cache_key(hours, context, getattr(analyzer, "provider", ""))
        if (is_ai and cache_seconds > 0)
        else None
    )
    if key:
        entry = _AI_CACHE.get(key)
        if entry and (time.monotonic() - entry["ts"]) < cache_seconds:
            return dict(entry["data"])
    try:
        result = analyzer.analyze(context)
        data = _as_dict(result)
        fallback = False
    except Exception as exc:
        reason = exc if isinstance(exc, AIAnalysisError) else type(exc).__name__
        logger.warning("AI 模型分析失败，已降级为规则分析：%s", reason)
        data, fallback = _fallback_analyzer(analyzer, context)
    if key and not fallback:
        _AI_CACHE[key] = {"ts": time.monotonic(), "data": dict(data)}
    return data


def _as_dict(result) -> dict:
    return result.to_dict() if isinstance(result, AIAnalysisOutput) else result


def _fallback_analyzer(analyzer: BaseAIAnalyzer, context: AIAnalysisContext) -> tuple:
    """分析器失败时的安全降级链（绝不向客户端抛 500）。

    - DoubaoAnalyzer → LocalWaterAnalyzer → RuleBasedAnalyzer；
    - openai / local → RuleBasedAnalyzer（保持既有 Stage 20B/20C 行为）。
    """
    if isinstance(analyzer, DoubaoAnalyzer):
        try:
            local = _as_dict(LocalWaterAnalyzer().analyze(context))
            seg = "豆包分析暂不可用，当前使用本地智能分析结果。"
            note = str(local.get("note") or "")
            local["note"] = (seg + " " + note) if note else seg
            return local, True
        except Exception as exc:
            logger.warning("本地智能分析降级同样失败，继续降级规则分析：%s", type(exc).__name__)
        rule = _as_dict(RuleBasedAnalyzer().analyze(context))
        seg = "豆包分析暂不可用，当前使用规则分析结果。"
        note = str(rule.get("note") or "")
        rule["note"] = (seg + " " + note) if note else seg
        return rule, True

    rule = _as_dict(RuleBasedAnalyzer().analyze(context))
    if analyzer.analysis_source == "local_intelligence":
        seg = "本地智能分析暂不可用，当前使用规则分析结果。"
    else:
        seg = "真实 AI 分析暂不可用，当前使用规则分析结果。"
    note = str(rule.get("note") or "")
    rule["note"] = (seg + " " + note) if note else seg
    return rule, True