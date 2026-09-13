"""Stage 20A：AI 智能水情分析测试。

覆盖 /api/ai-analysis 只读接口与 RuleBasedAnalyzer 规则分析：
- 正常返回与返回结构完整
- risk_level 合法、risk_score 在 0~100
- 空数据不 500
- 高水位提高风险等级
- 现有预警逻辑仍然生效
- analysis_source = rule_based（不调用外部 AI / 不出现 secret）
- 现有 API 回归不受影响
"""

import database
import main
from fastapi.testclient import TestClient
from services.ai_analysis_service import (
    AIAnalysisContext,
    RuleBasedAnalyzer,
    get_ai_analyzer,
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
    assert data["analysis_source"] in ("rule_based", "ai_model")


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