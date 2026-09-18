"""Stage 20A + 20B：AI 智能水情分析测试。

覆盖：
- /api/ai-analysis 只读接口与 RuleBasedAnalyzer 规则分析；
- OpenAIAnalyzer 真实 AI 分析器（Stage 20B）：选择、防抖降级、JSON 校验、
  安全修正、mock 数据免责声明、短时缓存；
- 真实 HTTP 一律通过 monkeypatch 模拟，绝不消耗真实 API quota；
- 验证 API Key 绝不进入响应与日志。
"""

import json

import database
import main
from fastapi.testclient import TestClient
import httpx
import services.ai_analysis_service as ai_service
from services.ai_analysis_service import (
    AIAnalysisContext,
    LocalWaterAnalyzer,
    OpenAIAnalyzer,
    RuleBasedAnalyzer,
    StationSnapshot,
    TrendSnapshot,
    build_analysis_context,
    get_ai_analyzer,
    reset_ai_analysis_cache,
)
from services.mock_water_provider import calculate_status

client = TestClient(main.app)

REQUIRED_KEYS = (
    "risk_level",
    "risk_score",
    "summary",
    "key_findings",
    "trend_analysis",
    "abnormal_stations",
    "recommendations",
    "generated_at",
    "analysis_source",
)
VALID_RISK_LEVELS = {"normal", "attention", "warning", "severe"}

_STATIONS = database.get_stations()
_WARNING_BY_ID = {s["station_id"]: s["warning_level"] for s in _STATIONS}


def _reset_alerts():
    conn = database.get_connection()
    conn.cursor().execute("DELETE FROM warning_records")
    conn.commit()
    conn.close()


def _latest_water(station_id: str, water_level: float, rainfall: float = 0.0):
    st = database.get_station_by_id(station_id)
    status = calculate_status(water_level, st["warning_level"], rainfall)
    database.insert_water_data(
        station_id=station_id,
        station_name=st["station_name"],
        water_level=water_level,
        warning_level=st["warning_level"],
        rainfall=rainfall,
        status=status,
    )


def _set_all_normal():
    for st in _STATIONS:
        _latest_water(st["station_id"], min(2.5, st["warning_level"] * 0.5), 0.0)


def _assert_ai_structure(data: dict):
    for key in REQUIRED_KEYS:
        assert key in data, f"缺少字段 {key}"
    assert data["risk_level"] in VALID_RISK_LEVELS
    assert isinstance(data["risk_score"], int)
    assert 0 <= data["risk_score"] <= 100
    assert isinstance(data["summary"], str) and data["summary"]
    assert isinstance(data["key_findings"], list) and data["key_findings"]
    assert isinstance(data["trend_analysis"], dict)
    assert isinstance(data["abnormal_stations"], list)
    assert isinstance(data["recommendations"], list)
    assert data["generated_at"]
    assert data["analysis_source"] in ("rule_based", "ai_model", "local_intelligence")


def test_ai_analysis_returns_200_and_structure():
    _reset_alerts()
    _set_all_normal()
    resp = client.get("/api/ai-analysis")
    assert resp.status_code == 200
    _assert_ai_structure(resp.json())


def test_risk_level_and_score_valid_always():
    _reset_alerts()
    for _ in range(3):
        resp = client.get("/api/ai-analysis")
        assert resp.status_code == 200
        data = resp.json()
        _assert_ai_structure(data)


def test_empty_data_no_500():
    result = RuleBasedAnalyzer().analyze(AIAnalysisContext())
    _assert_ai_structure(result)
    assert result["risk_level"] == "normal"
    assert result["risk_score"] == 0
    assert "暂无足够" in result["summary"] or "足够" in result["summary"]


def test_high_water_raises_risk_level():
    _reset_alerts()
    _set_all_normal()
    _latest_water("ST001", 5.2, 0.0)  # ST001 警戒水位 5.0，达到警戒
    resp = client.get("/api/ai-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["risk_level"] in ("warning", "severe")
    assert data["risk_score"] >= 72
    assert any(s["station_id"] == "ST001" for s in data["abnormal_stations"])


def test_existing_alert_logic_still_applies():
    _reset_alerts()
    _set_all_normal()
    # 直接依据现有预警服务创建预警（当前水位正常但存在未解除预警）
    from services.alert_service import create_alert_if_needed

    created = create_alert_if_needed(
        station_id="ST001",
        station_name="都江堰水文站",
        water_level=5.5,
        warning_level=_WARNING_BY_ID["ST001"],
        rainfall=0.0,
    )
    assert created is not None
    resp = client.get("/api/ai-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["risk_level"] == "attention"
    assert any("预警" in finding for finding in data["key_findings"])
    assert any("预警" in rec for rec in data["recommendations"])


def test_analysis_source_rule_based_no_external_ai():
    analyzer = get_ai_analyzer()
    assert isinstance(analyzer, RuleBasedAnalyzer)
    resp = client.get("/api/ai-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["analysis_source"] == "rule_based"
    lower = str(data).lower()
    for forbidden in ("openai", "claude", "gemini", "api_key", "secret", "sk-"):
        assert forbidden not in lower


def test_existing_api_regression_unaffected():
    _reset_alerts()
    _set_all_normal()
    assert client.get("/api/water-data?station_id=ST001").status_code == 200
    assert client.get("/api/history?station_id=ST001&hours=24").status_code == 200
    assert client.get("/api/alerts").status_code == 200
    assert client.get("/api/comparison?hours=24").status_code == 200
    assert client.get("/api/water-data-all").status_code == 200


def test_ai_analysis_hours_param_bounds():
    assert client.get("/api/ai-analysis?hours=0").status_code == 422
    assert client.get("/api/ai-analysis?hours=721").status_code == 422
    assert client.get("/api/ai-analysis?hours=24").status_code == 200


# ──────────────────────────── Stage 20B：OpenAIAnalyzer ────────────────────────────


def _fake_openai_resp(content: str):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    return FakeResp()


def _valid_ai_content(**overrides) -> str:
    base = {
        "risk_level": "attention",
        "risk_score": 55,
        "summary": "各站水位整体平稳，个别站点需适当关注。",
        "key_findings": ["ST001 水位略高于注意阈值"],
        "trend_analysis": {"overall": "过去 24 小时多数站点水位平稳。", "stations": []},
        "abnormal_stations": [],
        "recommendations": ["按常规频率继续监测水位变化。"],
        "generated_at": "2026-01-01T00:00:00",
        "analysis_source": "ai_model",
        "note": "",
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_ai_analyzer_openai_selected_when_env_set(monkeypatch):
    monkeypatch.delenv("AI_ANALYZER", raising=False)
    assert isinstance(get_ai_analyzer(), RuleBasedAnalyzer)
    monkeypatch.setenv("AI_ANALYZER", "openai")
    analyzer = get_ai_analyzer()
    assert isinstance(analyzer, OpenAIAnalyzer)
    assert analyzer.analysis_source == "ai_model"


def test_missing_api_key_falls_back_to_rule(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    _reset_alerts()
    _set_all_normal()
    resp = client.get("/api/ai-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["analysis_source"] == "rule_based"
    assert "真实 AI 分析暂不可用" in data["note"]


def test_openai_success_returns_structured(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_MODEL", "test-model-name")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    calls = []
    monkeypatch.setattr(
        ai_service.httpx,
        "post",
        lambda *a, **kw: (calls.append(kw), _fake_openai_resp(_valid_ai_content()))[1],
    )
    _reset_alerts()
    _set_all_normal()
    resp = client.get("/api/ai-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["analysis_source"] == "ai_model"
    assert data["model_name"] == "test-model-name"
    _assert_ai_structure(data)
    assert calls, "真实 AI 分析器应发起一次 OpenAI API 调用"


def test_openai_invalid_json_falls_back(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    monkeypatch.setattr(
        ai_service.httpx,
        "post",
        lambda *a, **kw: _fake_openai_resp("这不是 JSON 响应"),
    )
    _reset_alerts()
    _set_all_normal()
    resp = client.get("/api/ai-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["analysis_source"] == "rule_based"
    assert "真实 AI 分析暂不可用" in data["note"]


def test_openai_invalid_risk_level_falls_back(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    monkeypatch.setattr(
        ai_service.httpx,
        "post",
        lambda *a, **kw: _fake_openai_resp(_valid_ai_content(risk_level="critical")),
    )
    _reset_alerts()
    _set_all_normal()
    data = client.get("/api/ai-analysis").json()
    assert data["analysis_source"] == "rule_based"


def test_openai_invalid_risk_score_falls_back(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    for bad in ("high", 150, -5):
        monkeypatch.setattr(
            ai_service.httpx,
            "post",
            lambda *a, **kw: _fake_openai_resp(_valid_ai_content(risk_score=bad)),
        )
        data = client.get("/api/ai-analysis").json()
        assert data["analysis_source"] == "rule_based"


def test_openai_timeout_falls_back(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")

    def raise_timeout(*a, **kw):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(ai_service.httpx, "post", raise_timeout)
    _reset_alerts()
    _set_all_normal()
    data = client.get("/api/ai-analysis").json()
    assert data["analysis_source"] == "rule_based"
    assert "真实 AI 分析暂不可用" in data["note"]


def test_openai_http_500_falls_back(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    server_error = httpx.HTTPStatusError(
        "500 Internal Server Error", request=req, response=httpx.Response(500, request=req)
    )

    def raise_500(*a, **kw):
        raise server_error

    monkeypatch.setattr(ai_service.httpx, "post", raise_500)
    _reset_alerts()
    _set_all_normal()
    data = client.get("/api/ai-analysis").json()
    assert data["analysis_source"] == "rule_based"
    assert "真实 AI 分析暂不可用" in data["note"]


def test_api_key_not_in_response(monkeypatch):
    secret_key = "sk-FAKE-SECRET-KEY-12345"
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", secret_key)
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    monkeypatch.setattr(
        ai_service.httpx,
        "post",
        lambda *a, **kw: _fake_openai_resp("bad response"),
    )
    _reset_alerts()
    _set_all_normal()
    data = client.get("/api/ai-analysis").json()
    assert secret_key not in str(data).lower()


def test_api_key_not_in_logs(monkeypatch, caplog):
    secret_key = "sk-FAKE-SECRET-KEY-12345"
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", secret_key)
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")

    def raise_timeout(*a, **kw):
        raise httpx.TimeoutException("Read timed out")

    monkeypatch.setattr(ai_service.httpx, "post", raise_timeout)
    _reset_alerts()
    _set_all_normal()
    client.get("/api/ai-analysis")
    assert secret_key not in caplog.text
    assert "已降级为规则分析" in caplog.text


def test_mock_data_not_called_official():
    _reset_alerts()
    _set_all_normal()
    data = client.get("/api/ai-analysis").json()
    assert "模拟数据" in data["note"]
    lower = str(data).lower()
    assert "成都实时水情" not in lower
    assert "官方实时" not in lower


def test_ai_cannot_lower_system_severe_risk(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    # AI 声称一切正常，但系统规则已判定 ST001 超警
    monkeypatch.setattr(
        ai_service.httpx,
        "post",
        lambda *a, **kw: _fake_openai_resp(_valid_ai_content(risk_level="normal", risk_score=20)),
    )
    _reset_alerts()
    _set_all_normal()
    _latest_water("ST001", 5.5, 0.0)  # 5.5 >= 5.0*1.1 → 超警
    data = client.get("/api/ai-analysis").json()
    assert data["analysis_source"] == "ai_model"
    assert data["risk_level"] == "severe"
    assert data["risk_score"] >= 95
    assert any(s["station_id"] == "ST001" for s in data["abnormal_stations"])


def test_ai_model_cache_reuses_result(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "60")
    reset_ai_analysis_cache()
    calls = {"n": 0}
    monkeypatch.setattr(
        ai_service.httpx,
        "post",
        lambda *a, **kw: (calls.__setitem__("n", calls["n"] + 1), _fake_openai_resp(_valid_ai_content()))[1],
    )
    _reset_alerts()
    _set_all_normal()
    _latest_water("ST001", 1.11, 0.0)  # 唯一状态，避免与其他测试指纹冲突
    assert client.get("/api/ai-analysis").json()["analysis_source"] == "ai_model"
    assert client.get("/api/ai-analysis").json()["analysis_source"] == "ai_model"
    assert calls["n"] == 1, "相同数据状态短时间内不应重复调用真实模型"
    _latest_water("ST001", 1.22, 0.0)  # 数据状态变化，指纹变化，应重新调用
    assert client.get("/api/ai-analysis").json()["analysis_source"] == "ai_model"
    assert calls["n"] == 2
    reset_ai_analysis_cache()


def test_rule_based_analyzer_still_works_with_openai_env(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    _reset_alerts()
    _set_all_normal()
    context = ai_service.build_analysis_context(hours=24)
    result = RuleBasedAnalyzer().analyze(context)
    _assert_ai_structure(result)
    assert result["analysis_source"] == "rule_based"


# ──────────────────────────── Stage 20C：LocalWaterAnalyzer ────────────────────────────


def _local_context(stations, trends=None, rainfall=None, forecasts=None, alerts=None):
    return AIAnalysisContext(
        hours=24,
        stations=stations,
        trends=trends or {},
        rainfall_trends=rainfall or {},
        forecasts=forecasts or {},
        alerts=alerts or [],
        alert_summary={},
    )


def test_local_analyzer_selected_with_env(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "local")
    assert isinstance(get_ai_analyzer(), LocalWaterAnalyzer)


def test_local_analysis_returns_200_and_structure(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "local")
    _reset_alerts()
    _set_all_normal()
    resp = client.get("/api/ai-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["analysis_source"] == "local_intelligence"
    _assert_ai_structure(data)
    assert data["report"]


def test_local_empty_data_no_500(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "local")
    data = LocalWaterAnalyzer().analyze(AIAnalysisContext())
    _assert_ai_structure(data)
    assert data["risk_level"] == "normal"
    assert "足够" in data["summary"]


def test_local_high_water_keeps_authoritative_risk(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "local")
    _reset_alerts()
    _set_all_normal()
    _latest_water("ST001", 5.2, 0.0)  # 警戒 5.0，达警戒
    data = client.get("/api/ai-analysis").json()
    assert data["risk_level"] in ("warning", "severe")
    assert data["risk_score"] >= 72
    assert any(s["station_id"] == "ST001" for s in data["abnormal_stations"])
    rule = RuleBasedAnalyzer().analyze(build_analysis_context(hours=24))
    assert data["risk_level"] == rule["risk_level"]
    assert data["risk_score"] == rule["risk_score"]


def test_local_strong_rain_flags_station(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "local")
    _reset_alerts()
    _set_all_normal()
    _latest_water("ST003", 2.5, 60.0)
    data = client.get("/api/ai-analysis").json()
    assert any(a["station_id"] == "ST003" for a in data["abnormal_stations"])
    assert data["rainfall_analysis"]["total_all"] >= 50
    assert any(
        i["station_id"] == "ST003" and i["total_rainfall"] >= 50
        for i in data["rainfall_analysis"]["stations"]
    )


def test_local_report_reflects_real_numbers():
    ctx = _local_context(
        stations=[
            StationSnapshot(
                station_id="ST001", station_name="都江堰水文站",
                water_level=4.61, warning_level=5.0, rainfall=3.2,
                status="注意", source="mock", data_quality="valid",
            ),
        ],
        trends={"ST001": TrendSnapshot(
            direction="rising", slope=0.03, change=0.18,
            recent_values=[4.4, 4.5, 4.61], window_hours=24,
        )},
        rainfall={"ST001": {"total": 23.5, "direction": "rising", "recent_values": [1, 2, 3]}},
        forecasts={"ST001": {
            "sufficient": True, "predicted_water_level": 4.78,
            "trend": "rising", "risk_level": "attention",
            "horizon_hours": 6, "current_water_level": 4.61,
        }},
    )
    data = LocalWaterAnalyzer().analyze(ctx)
    _assert_ai_structure(data)
    assert "4.61" in data["report"]
    assert "5.00" in data["report"]
    assert "23.5" in data["report"]
    assert data["risk_reasons"]
    assert data["future_outlook"]
    assert data["key_findings"]


def test_local_report_changes_with_data_and_deterministic():
    ctx1 = _local_context(
        stations=[StationSnapshot(
            station_id="ST001", station_name="都江堰水文站",
            water_level=4.61, warning_level=5.0, rainfall=3.2,
            status="注意", source="mock", data_quality="valid",
        )],
        trends={"ST001": TrendSnapshot(
            direction="rising", slope=0.03, change=0.18,
            recent_values=[4.4, 4.5, 4.61], window_hours=24,
        )},
        rainfall={"ST001": {"total": 23.5, "direction": "rising", "recent_values": [1, 2, 3]}},
        forecasts={"ST001": {
            "sufficient": True, "predicted_water_level": 4.78,
            "trend": "rising", "risk_level": "attention",
            "horizon_hours": 6, "current_water_level": 4.61,
        }},
    )
    ctx2 = _local_context(
        stations=[StationSnapshot(
            station_id="ST001", station_name="都江堰水文站",
            water_level=2.5, warning_level=5.0, rainfall=0.0,
            status="正常", source="mock", data_quality="valid",
        )],
        trends={"ST001": TrendSnapshot(
            direction="falling", slope=-0.05, change=-0.3,
            recent_values=[2.8, 2.6, 2.5], window_hours=24,
        )},
        rainfall={"ST001": {"total": 0.0, "direction": "none", "recent_values": []}},
    )
    r1a = LocalWaterAnalyzer().analyze(ctx1)
    r1b = LocalWaterAnalyzer().analyze(ctx1)
    r2 = LocalWaterAnalyzer().analyze(ctx2)
    assert r1a["report"] == r1b["report"], "同一数据必须生成相同文案（确定性）"
    assert r1a["report"] != r2["report"], "数据变化后分析结果应随之变化"
    assert "上涨" in r1a["report"]
    assert "下降" in r2["report"]


def test_local_fallback_to_rule_on_exception(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "local")
    _reset_alerts()
    _set_all_normal()

    def boom(self, context):
        raise RuntimeError("local analyzer crashed")

    monkeypatch.setattr(ai_service.LocalWaterAnalyzer, "analyze", boom)
    data = client.get("/api/ai-analysis").json()
    assert data["analysis_source"] == "rule_based"
    assert "规则分析结果" in data["note"]
    _assert_ai_structure(data)


def test_local_does_not_degrade_severe():
    ctx = _local_context(
        stations=[StationSnapshot(
            station_id="ST001", station_name="都江堰水文站",
            water_level=6.2, warning_level=5.0, rainfall=0.0,
            status="超警", source="mock", data_quality="valid",
        )],
    )
    data = LocalWaterAnalyzer().analyze(ctx)
    assert data["risk_level"] == "severe"
    assert data["risk_score"] >= 95
    assert "超警" in data["report"]


def test_local_multi_station_abnormal_detected():
    stations = [
        StationSnapshot(
            station_id="ST001", station_name="都江堰水文站",
            water_level=6.2, warning_level=5.0, rainfall=0.0,
            status="超警", source="mock", data_quality="valid",
        ),
        StationSnapshot(
            station_id="ST002", station_name="望江楼水文站",
            water_level=4.4, warning_level=5.0, rainfall=0.0,
            status="注意", source="mock", data_quality="valid",
        ),
    ]
    data = LocalWaterAnalyzer().analyze(_local_context(stations=stations))
    ids = {s["station_id"] for s in data["abnormal_stations"]}
    assert ids == {"ST001", "ST002"}
    assert any("都江堰" in r for r in data["risk_reasons"])


def test_local_rising_and_falling_trends_reflected():
    base = StationSnapshot(
        station_id="ST001", station_name="都江堰水文站",
        water_level=4.5, warning_level=5.0, rainfall=0.0,
        status="注意", source="mock", data_quality="valid",
    )
    rising = _local_context(
        stations=[base],
        trends={"ST001": TrendSnapshot(
            direction="rising", slope=0.05, change=0.2,
            recent_values=[4.3, 4.4, 4.5], window_hours=24,
        )},
    )
    falling = _local_context(
        stations=[base],
        trends={"ST001": TrendSnapshot(
            direction="falling", slope=-0.05, change=-0.2,
            recent_values=[4.7, 4.6, 4.5], window_hours=24,
        )},
    )
    d1 = LocalWaterAnalyzer().analyze(rising)
    d2 = LocalWaterAnalyzer().analyze(falling)
    assert d1["trend_analysis"]["stations"][0]["direction"] == "rising"
    assert d2["trend_analysis"]["stations"][0]["direction"] == "falling"
    assert "上涨" in d1["trend_analysis"]["stations"][0]["description"]
    assert "下降" in d2["trend_analysis"]["stations"][0]["description"]


def test_local_no_history_data_reports_insufficient():
    ctx = _local_context(
        stations=[StationSnapshot(
            station_id="ST001", station_name="都江堰水文站",
            water_level=4.5, warning_level=5.0, rainfall=0.0,
            status="注意", source="mock", data_quality="valid",
        )],
    )
    data = LocalWaterAnalyzer().analyze(ctx)
    _assert_ai_structure(data)
    assert data["future_outlook"] == "历史数据不足，暂无法提供未来趋势预判。"
    assert data["trend_analysis"]["stations"][0]["direction"] == "insufficient"


def test_rule_and_openai_unaffected_by_local_source(monkeypatch):
    assert isinstance(get_ai_analyzer(), RuleBasedAnalyzer)
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _reset_alerts()
    _set_all_normal()
    data = client.get("/api/ai-analysis").json()
    assert data["analysis_source"] == "rule_based"
    assert "真实 AI 分析暂不可用" in data["note"]
    reset_ai_analysis_cache()