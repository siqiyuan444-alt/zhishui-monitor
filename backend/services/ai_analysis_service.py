"""AI 智能水情分析服务（Stage 20A 架构 + Stage 20B 真实 AI 接入）。

架构：
- BaseAIAnalyzer 统一分析器接口，前端与 main.py 只依赖该接口；
- RuleBasedAnalyzer：复用现有水利规则 calculate_status() 的规则分析，
  analysis_source = "rule_based"，作为默认与降级方案；
- OpenAIAnalyzer：真实 AI 模型分析（Stage 20B），analysis_source = "ai_model"，
  通过环境变量 AI_ANALYZER=openai 启用，失败时由 generate_ai_analysis() 自动
  降级为 RuleBasedAnalyzer，绝不因 AI 失败导致水情监测系统不可用。

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
from services.data_analysis import build_comparison
from services.mock_water_provider import calculate_status

logger = logging.getLogger("water_monitor.ai_analysis")

# 合法的 AI 风险等级 / 分析来源取值
RISK_LEVELS = ("normal", "attention", "warning", "severe")
ANALYSIS_SOURCES = ("rule_based", "ai_model")
RISK_ORDER = {"normal": 0, "attention": 1, "warning": 2, "severe": 3}

# 真实 AI 调用默认配置（环境变量同名覆盖；仅为可选功能，不配置不影响系统运行）
DEFAULT_AI_MODEL = "gpt-4o-mini"
DEFAULT_AI_TIMEOUT = 30
DEFAULT_AI_CACHE_SECONDS = 60

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
    model_name: str = ""

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
        return data


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


def _cache_key(hours: int, context: "AIAnalysisContext") -> str:
    """以 hours + 当前各站最新数据状态 + 未解除预警集合作为指纹。"""
    states = sorted(
        f"{s.station_id}:{_to_float(s.water_level):.3f}:{_to_float(s.rainfall):.1f}:{s.collected_at or ''}"
        for s in (context.stations or [])
    )
    alert_ids = sorted(str(a.id) for a in (context.alerts or []))
    fingerprint = hashlib.sha256(
        "|".join(states + [f"alerts:{alert_ids}"]).encode("utf-8")
    ).hexdigest()[:16]
    return f"ai-analysis:{int(hours)}:{fingerprint}"


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


def _build_system_prompt() -> str:
    return (
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
        "8. 输出只允许 JSON，不要包含 JSON 之外的解释文字或代码围栏。"
    )


def _build_user_prompt(context: AIAnalysisContext) -> str:
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
    return json.dumps(payload, ensure_ascii=False)


def _parse_and_validate(content: str) -> dict:
    """解析 AI 返回内容并做 Schema 校验；失败抛出 AIAnalysisError。"""
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
    return _validate_output(raw)


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


# ──────────────────────────── 分析器工厂 ────────────────────────────


def get_ai_analyzer(name: str = None) -> BaseAIAnalyzer:
    """根据配置创建分析器实例。

    未指定时读取环境变量 AI_ANALYZER（默认 rule_based）。
    - rule_based：默认规则分析，不调用任何外部 API；
    - openai：OpenAI 兼容真实 AI 分析器（需配置 OPENAI_API_KEY，失败自动降级）。
    """
    analyzer_name = (name or os.environ.get("AI_ANALYZER", "rule_based")).strip().lower()
    if analyzer_name in ("rule_based", "rule", "规则"):
        return RuleBasedAnalyzer()
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
    """读取当前系统水情数据 → 调用所选分析器 → 返回结构化结果（不写库）。

    - 默认 rule_based，不产生任何外部 AI API 调用；
    - AI_ANALYZER=openai 时调用真实模型；任何失败自动降级 RuleBasedAnalyzer，
      并在 note 中说明“真实 AI 分析暂不可用，当前使用规则分析结果”；
    - 仅对真实模型成功结果做短时缓存（AI_ANALYSIS_CACHE_SECONDS 秒，默认 60），
      相同 hours + 数据状态短时间内不重复调用模型。
    """
    context = build_analysis_context(hours=hours)
    analyzer = get_ai_analyzer()
    is_ai = analyzer.analysis_source == "ai_model"
    cache_seconds = _cache_seconds()
    key = _cache_key(hours, context) if (is_ai and cache_seconds > 0) else None
    if key:
        entry = _AI_CACHE.get(key)
        if entry and (time.monotonic() - entry["ts"]) < cache_seconds:
            return dict(entry["data"])
    try:
        result = analyzer.analyze(context)
        data = result.to_dict() if isinstance(result, AIAnalysisOutput) else result
        fallback = False
    except Exception as exc:
        reason = exc if isinstance(exc, AIAnalysisError) else type(exc).__name__
        logger.warning("AI 模型分析失败，已降级为规则分析：%s", reason)
        rule_result = RuleBasedAnalyzer().analyze(context)
        rule = rule_result.to_dict() if isinstance(rule_result, AIAnalysisOutput) else rule_result
        fallback_note = "真实 AI 分析暂不可用，当前使用规则分析结果。"
        note = str(rule.get("note") or "")
        rule["note"] = (fallback_note + " " + note) if note else fallback_note
        data = rule
        fallback = True
    if key and not fallback:
        _AI_CACHE[key] = {"ts": time.monotonic(), "data": dict(data)}
    return data