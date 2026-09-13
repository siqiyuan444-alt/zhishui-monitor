"""Stage 18B：成都官方真实雨情 API 接入测试。

覆盖：
1. HMAC-SHA256 签名正确
2. Base64 正确
3. X-Client-Id 正确
4. X-Timestamp 正确
5. X-Nonce 每次动态生成
6. stcd 正确传递
7. 官方 JSON 正确解析
8. drp 正确解析
9. dyp 正确解析
10. tm 正确解析
11. 缺少 Client ID
12. 缺少 Client Secret
13. timeout
14. HTTP 500
15. JSON 异常
16. 空数据
17. 自动 Mock fallback
18. 原有水情接口回归
19. 原有预警接口回归
20. 原有报表接口回归
"""

import json
import sys
import os
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import main
import services.real_water_provider as rwp

client = TestClient(main.app)


# ──────────────────────────── 1-2. 签名算法 ────────────────────────────

def test_hmac_signature_matches_known_vector():
    sig = rwp.build_signature("cid123", "1700000000000", "NONCE_v1", "sec456")
    assert sig == "TbYntHd40grcCVz1j0zkzuSNJltb0PdvGM3WzcEwgTI="


def test_signature_is_base64():
    sig = rwp.build_signature("client-a", "1", "nonce-a", "secret")
    import base64

    decoded = base64.b64decode(sig, validate=True)
    assert len(decoded) == 32  # SHA256 digest


def test_signature_changes_with_timestamp():
    s1 = rwp.build_signature("c", "t1", "n", "s")
    s2 = rwp.build_signature("c", "t2", "n", "s")
    assert s1 != s2


# ──────────────────────────── 3-6. 请求头 / stcd ────────────────────────────

def _make_provider(monkeypatch, **env):
    base = {
        "CHENGDU_API_BASE_URL": "https://example.test/api",
        "CHENGDU_CLIENT_ID": "test-client",
        "CHENGDU_CLIENT_SECRET": "test-secret",
        "CHENGDU_API_TIMEOUT": "10",
        "CHENGDU_STATION_CODES": json.dumps({"ST001": "510101", "ST002": "510102", "ST005": "510105"}),
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    return rwp.RealWaterProvider()


def test_request_headers_and_stcd_are_correct(monkeypatch):
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return {"data": [{"stcd": "510101", "tm": "2026-09-12 10:00:00", "drp": 12.5, "dyp": 30}]}

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(monkeypatch)
    provider.get_station_data("ST001")

    assert captured["params"] == {"stcd": "510101"}
    assert captured["url"] == "https://example.test/api"
    assert captured["timeout"] == 10
    assert captured["headers"]["X-Client-Id"] == "test-client"

    ts = captured["headers"]["X-Timestamp"]
    assert ts.isdigit() and len(ts) == 13  # 毫秒时间戳

    expected_sig = rwp.build_signature(
        "test-client", ts, captured["headers"]["X-Nonce"], "test-secret"
    )
    assert captured["headers"]["X-Signature"] == expected_sig


def test_nonce_is_dynamic_per_request(monkeypatch):
    nonces = []

    def fake_get(url, params, headers, timeout):
        nonces.append(headers["X-Nonce"])
        return {"data": [{"stcd": "510101", "tm": "2026-09-12 10:00:00", "drp": 5}]}

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(monkeypatch)
    provider.get_station_data("ST001")
    provider.get_station_data("ST001")
    assert len(nonces) == 2
    assert nonces[0] != nonces[1]


def test_timestamp_is_dynamic_per_request(monkeypatch):
    import time

    timestamps = []

    def fake_get(url, params, headers, timeout):
        timestamps.append(headers["X-Timestamp"])
        return {"data": [{"stcd": "510101", "tm": "2026-09-12 10:00:00", "drp": 5}]}

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(monkeypatch)
    provider.get_station_data("ST001")
    time.sleep(0.01)
    provider.get_station_data("ST001")
    assert len(timestamps) == 2
    assert timestamps[0] != timestamps[1]


# ──────────────────────────── 7-10. 官方 JSON 解析 ────────────────────────────

SAMPLE_RECORD = {
    "stcd": "510101",
    "tm": "2026-09-12 10:00:00",
    "pdr": 2,
    "last_modify_time": "2026-09-12 10:05:00",
    "id": "abc123",
    "flag": 0,
    "dyp": 30.0,
    "drp": 12.5,
    "intv": 1,
    "wth": "雨",
}


def test_official_json_parsed_correctly(monkeypatch):
    monkeypatch.setattr(
        "services.real_water_provider._http_get_json",
        lambda *a, **k: {"data": [SAMPLE_RECORD]},
    )
    provider = _make_provider(monkeypatch)
    record = provider.get_station_data("ST001")
    assert record["station_id"] == "ST001"
    assert record["source"] == "chengdu_open_data"
    assert record["data_quality"] == "good"
    assert record["rainfall_origin"] == "drp"
    assert record["official:stcd"] == "510101"


def test_drp_parsed_as_rainfall(monkeypatch):
    monkeypatch.setattr(
        "services.real_water_provider._http_get_json",
        lambda *a, **k: {"result": [SAMPLE_RECORD]},
    )
    provider = _make_provider(monkeypatch)
    record = provider.get_station_data("ST001")
    assert record["rainfall"] == 12.5
    assert record["data_quality"] == "good"


def test_dyp_used_when_drp_missing(monkeypatch):
    rec = dict(SAMPLE_RECORD)
    rec["drp"] = None
    monkeypatch.setattr(
        "services.real_water_provider._http_get_json",
        lambda *a, **k: {"data": [rec]},
    )
    provider = _make_provider(monkeypatch)
    record = provider.get_station_data("ST001")
    assert record["rainfall"] == 30.0
    assert record["rainfall_origin"] == "dyp"
    assert record["data_quality"] == "degraded"


def test_tm_parsed_as_timestamp(monkeypatch):
    monkeypatch.setattr(
        "services.real_water_provider._http_get_json",
        lambda *a, **k: {"records": [SAMPLE_RECORD]},
    )
    provider = _make_provider(monkeypatch)
    record = provider.get_station_data("ST001")
    assert record["timestamp"].startswith("2026-09-12T10:00:00")


def test_rainfall_never_mapped_to_water_level(monkeypatch):
    monkeypatch.setattr(
        "services.real_water_provider._http_get_json",
        lambda *a, **k: {"data": [SAMPLE_RECORD]},
    )
    provider = _make_provider(monkeypatch)
    record = provider.get_station_data("ST001")
    assert record["water_level"] != 12.5
    assert record["water_level"] != 30.0


# ──────────────────────────── 11-12. 缺少凭据 ────────────────────────────

def test_missing_client_id_raises_not_configured(monkeypatch):
    monkeypatch.setenv("CHENGDU_CLIENT_ID", "")
    monkeypatch.setenv("CHENGDU_CLIENT_SECRET", "sec")
    monkeypatch.delenv("CHENGDU_API_BASE_URL", raising=False)
    provider = rwp.RealWaterProvider()
    assert not provider.is_configured()
    with pytest.raises(rwp.ProviderNotConfiguredError):
        provider.get_current_data()


def test_missing_client_secret_raises_not_configured(monkeypatch):
    monkeypatch.setenv("CHENGDU_CLIENT_ID", "cid")
    monkeypatch.setenv("CHENGDU_CLIENT_SECRET", "")
    provider = rwp.RealWaterProvider()
    assert not provider.is_configured()
    with pytest.raises(rwp.ProviderNotConfiguredError):
        provider.get_current_data()


def test_missing_stcd_cannot_fake_success(monkeypatch):
    provider = _make_provider(monkeypatch, CHENGDU_STATION_CODES="{}")
    assert provider.get_station_code("ST003") == ""
    with pytest.raises(rwp.ProviderNotConfiguredError):
        provider.get_station_data("ST003")


# ──────────────────────────── 13-16. 异常 / 兜底 ────────────────────────────

@pytest.mark.parametrize("kind,exc", [
    ("timeout", rwp.ChengduApiError("timeout", "超时")),
    ("http", rwp.ChengduApiError("http", "HTTP 500")),
    ("json", rwp.ChengduApiError("json", "JSON 无效")),
    ("network", rwp.ChengduApiError("network", "DNS 失败")),
])
def test_request_level_errors_raise_for_wholesale_fallback(monkeypatch, kind, exc):
    def fake_get(*a, **k):
        raise exc

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(monkeypatch)
    from services.water_data_provider import ProviderError

    with pytest.raises(ProviderError):
        provider.get_current_data()


def test_empty_data_becomes_invalid_fallback(monkeypatch):
    monkeypatch.setattr(
        "services.real_water_provider._http_get_json",
        lambda *a, **k: {"data": []},
    )
    provider = _make_provider(monkeypatch)
    record = provider.get_station_data("ST001")
    assert record["source"] == "invalid_fallback"
    assert record["data_quality"] == "degraded"


def test_missing_fields_becomes_invalid_fallback(monkeypatch):
    monkeypatch.setattr(
        "services.real_water_provider._http_get_json",
        lambda *a, **k: {"data": [{"stcd": "510101", "tm": "2026-09-12 10:00:00"}]},
    )
    provider = _make_provider(monkeypatch)
    record = provider.get_station_data("ST001")
    assert record["source"] == "invalid_fallback"
    assert record["data_quality"] == "degraded"


# ──────────────────────────── 17. 自动 Mock fallback（采集器） ────────────────────────────

def test_collector_falls_back_to_mock_when_real_fails(monkeypatch):
    from services.data_collector import DataCollector
    from services.mock_water_provider import MockWaterProvider

    def fake_get(*a, **k):
        raise rwp.ChengduApiError("http", "HTTP 503")

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(monkeypatch)
    collector = DataCollector(provider=provider)
    report = collector.collect_all()

    assert report["status"] == "fallback"
    assert report["source"] == "mock_fallback"
    assert report["record_count"] == 5
    for rec in report["records"]:
        assert rec["source"] == "mock_fallback"
        assert rec["data_quality"] == "degraded"

    # Mock 提供器本身不受影响
    mock = MockWaterProvider().get_current_data()
    assert len(mock) == 5
    assert "source" not in mock[0]


def test_collector_with_mock_provider_keeps_mock_branding(monkeypatch):
    from services.data_collector import DataCollector

    collector = DataCollector()  # 默认/测试环境为 mock
    report = collector.collect_all()
    assert report["source"] == "mock"
    assert report["status"] == "active"
    for rec in report["records"]:
        assert rec["source"] == "mock"
        assert rec["data_quality"] == "valid"


def test_collector_uses_per_record_source_overrides(monkeypatch):
    from services.data_collector import DataCollector
    from services.mock_water_provider import MockWaterProvider

    class MixedProvider(MockWaterProvider):
        name = "chengdu_open_data"

        def get_current_data(self):
            records = super().get_current_data()
            records[0]["source"] = "chengdu_open_data"
            records[0]["data_quality"] = "good"
            return records

    collector = DataCollector(provider=MixedProvider())
    report = collector.collect_all()
    assert report["records"][0]["source"] == "chengdu_open_data"
    assert report["records"][0]["data_quality"] == "good"
    assert report["records"][1]["source"] == "chengdu_open_data"


def test_get_provider_auto_selects_chengdu_when_credentials_present(monkeypatch):
    from services.water_data_provider import get_provider

    monkeypatch.delenv("WATER_DATA_PROVIDER", raising=False)
    monkeypatch.setenv("CHENGDU_CLIENT_ID", "cid")
    monkeypatch.setenv("CHENGDU_CLIENT_SECRET", "sec")
    provider = get_provider()
    assert provider.name == "chengdu_open_data"


def test_get_provider_mock_when_no_credentials(monkeypatch):
    from services.water_data_provider import get_provider

    monkeypatch.delenv("WATER_DATA_PROVIDER", raising=False)
    monkeypatch.delenv("CHENGDU_CLIENT_ID", raising=False)
    monkeypatch.delenv("CHENGDU_CLIENT_SECRET", raising=False)
    provider = get_provider()
    assert provider.name == "mock"


# ──────────────────────────── 客户端接口回归（真实 DNS 挂载测试不发出请求） ────────────────────────────

def test_data_source_endpoint(monkeypatch):
    monkeypatch.delenv("WATER_DATA_PROVIDER", raising=False)
    monkeypatch.delenv("CHENGDU_CLIENT_ID", raising=False)
    monkeypatch.delenv("CHENGDU_CLIENT_SECRET", raising=False)
    resp = client.get("/api/data-source")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "mock"
    assert body["configured"] is True


def test_data_quality_endpoint():
    resp = client.get("/api/data-quality")
    assert resp.status_code == 200
    body = resp.json()
    assert "data_quality" in body
    assert "total" in body["data_quality"]


def test_water_data_api_regression_existing():
    resp = client.get("/api/water-data?station_id=ST001")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("water_level", "warning_level", "rainfall", "status", "source", "data_quality"):
        assert key in body


def test_water_data_all_api_regression_existing():
    resp = client.get("/api/water-data-all")
    assert resp.status_code == 200


def test_warnings_api_regression_existing():
    resp = client.get("/api/warnings?limit=5")
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_alerts_api_regression_existing():
    resp = client.get("/api/alerts")
    assert resp.status_code == 200


def test_report_api_regression_existing():
    resp = client.get("/api/report?station_id=ST001&hours=24")
    assert resp.status_code == 200
    assert "data" in resp.json()


# ──────────────────────────── 18C: 连接测试（check_connection） ────────────────────────────

def _assert_no_http_request(url, params, headers, timeout):
    raise AssertionError("未配置真实 stcd 时禁止发起网络请求")


def test_connectivity_missing_client_id(monkeypatch):
    provider = _make_provider(monkeypatch, CHENGDU_CLIENT_ID="", CHENGDU_STATION_CODES="{}")
    monkeypatch.setattr("services.real_water_provider._http_get_json", _assert_no_http_request)
    status = provider.check_connection()
    assert status["configured"] is False
    assert status["signature_ok"] is False
    assert status["reachable"] is False
    assert status["tested_stcd"] is False
    assert "CHENGDU_CLIENT_ID" in status["reason"]


def test_connectivity_missing_client_secret(monkeypatch):
    provider = _make_provider(monkeypatch, CHENGDU_CLIENT_SECRET="", CHENGDU_STATION_CODES="{}")
    monkeypatch.setattr("services.real_water_provider._http_get_json", _assert_no_http_request)
    status = provider.check_connection()
    assert status["configured"] is False
    assert "CHENGDU_CLIENT_SECRET" in status["reason"]


def test_connectivity_missing_base_url(monkeypatch):
    provider = _make_provider(monkeypatch, CHENGDU_STATION_CODES="{}")
    provider.base_url = ""
    monkeypatch.setattr("services.real_water_provider._http_get_json", _assert_no_http_request)
    status = provider.check_connection()
    assert status["configured"] is False
    assert "CHENGDU_API_BASE_URL" in status["reason"]


def test_connectivity_no_stcd_does_not_call_api(monkeypatch):
    provider = _make_provider(monkeypatch, CHENGDU_STATION_CODES="{}")
    monkeypatch.setattr("services.real_water_provider._http_get_json", _assert_no_http_request)
    status = provider.check_connection()
    assert status["configured"] is True
    assert status["signature_ok"] is True
    assert status["reachable"] is False
    assert status["authenticated"] is False
    assert status["data_valid"] is False
    assert status["tested_stcd"] is False
    assert "stcd" in status["reason"]


def test_connectivity_no_stcd_does_not_leak_secret(monkeypatch):
    provider = _make_provider(
        monkeypatch, CHENGDU_CLIENT_SECRET="must-not-leak-abc", CHENGDU_STATION_CODES="{}"
    )
    monkeypatch.setattr("services.real_water_provider._http_get_json", _assert_no_http_request)
    status = provider.check_connection()
    text = json.dumps(status, ensure_ascii=False)
    assert "must-not-leak-abc" not in text
    assert "X-Signature" not in text
    assert "X-Timestamp" not in text
    assert "X-Nonce" not in text


def test_connectivity_success_with_real_stcd(monkeypatch):
    payload = {"data": [{"stcd": "REAL_TEST_STCD", "tm": "2026-09-12 20:00:00", "drp": 2.5, "dyp": 12.5}]}
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["params"] = params
        return payload

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(
        monkeypatch, CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"})
    )
    status = provider.check_connection()
    assert captured["params"] == {"stcd": "REAL_TEST_STCD"}
    assert status["configured"] is True
    assert status["signature_ok"] is True
    assert status["reachable"] is True
    assert status["authenticated"] is True
    assert status["data_valid"] is True
    assert status["data_quality"] == "good"
    assert status["tested_stcd"] is True


def test_connectivity_empty_data_marks_invalid(monkeypatch):
    monkeypatch.setattr("services.real_water_provider._http_get_json", lambda *a, **k: {"data": []})
    provider = _make_provider(
        monkeypatch, CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"})
    )
    status = provider.check_connection()
    assert status["configured"] is True
    assert status["reachable"] is True
    assert status["authenticated"] is True
    assert status["data_valid"] is False
    assert status["tested_stcd"] is True


def test_connectivity_http_401_authenticated_false(monkeypatch):
    def fake_get(*a, **k):
        raise rwp.ChengduApiError("http", "HTTP 401", status_code=401)

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(
        monkeypatch, CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"})
    )
    status = provider.check_connection()
    assert status["configured"] is True
    assert status["reachable"] is True
    assert status["authenticated"] is False
    assert status["data_valid"] is False


def test_connectivity_timeout_marks_unreachable(monkeypatch):
    def fake_get(*a, **k):
        raise rwp.ChengduApiError("timeout", "超时")

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(
        monkeypatch, CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"})
    )
    status = provider.check_connection()
    assert status["configured"] is True
    assert status["signature_ok"] is True
    assert status["reachable"] is False
    assert status["tested_stcd"] is True


# ──────────────────────────── 18C: 异常保护（HTTP 400/401/403 + 字段缺失） ────────────────────────────

@pytest.mark.parametrize("code", [400, 401, 403, 500])
def test_http_error_statuses_trigger_fallback(monkeypatch, code):
    from services.data_collector import DataCollector

    def fake_get(*a, **k):
        raise rwp.ChengduApiError("http", f"HTTP {code}", status_code=code)

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(
        monkeypatch, CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"})
    )
    collector = DataCollector(provider=provider)
    report = collector.collect_all()
    assert report["status"] == "fallback"
    assert report["source"] == "mock_fallback"
    for rec in report["records"]:
        assert rec["source"] == "mock_fallback"
        assert rec["data_quality"] == "degraded"


def test_missing_tm_falls_back_to_now(monkeypatch):
    rec = {"stcd": "REAL_TEST_STCD", "drp": 3.0, "dyp": 9.0}
    monkeypatch.setattr("services.real_water_provider._http_get_json", lambda *a, **k: {"data": [rec]})
    provider = _make_provider(
        monkeypatch, CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"})
    )
    record = provider.get_station_data("ST001")
    assert record["source"] == "chengdu_open_data"
    assert record["rainfall"] == 3.0
    parsed = datetime.fromisoformat(record["timestamp"])
    assert parsed.year >= 2020  # 未提供 tm 时回退到当前时间而非崩溃


def test_missing_wth_and_optional_fields_still_ok(monkeypatch):
    rec = {"stcd": "REAL_TEST_STCD", "tm": "2026-09-12 20:00:00", "drp": 1.2}
    monkeypatch.setattr("services.real_water_provider._http_get_json", lambda *a, **k: {"data": [rec]})
    provider = _make_provider(
        monkeypatch, CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"})
    )
    record = provider.get_station_data("ST001")
    assert record["rainfall"] == 1.2
    assert record["official:wth"] is None


def test_collector_no_stcd_auto_fallback(monkeypatch):
    from services.data_collector import DataCollector

    provider = _make_provider(monkeypatch, CHENGDU_STATION_CODES="{}")
    collector = DataCollector(provider=provider)
    report = collector.collect_all()
    assert report["status"] == "fallback"
    assert report["source"] == "mock_fallback"
    assert report["record_count"] == 5
    for rec in report["records"]:
        assert rec["source"] == "mock_fallback"
        assert rec["data_quality"] == "degraded"


# ──────────────────────────── 18C: 成功数据 + 签名安全 ────────────────────────────

def test_real_test_stcd_success_fields(monkeypatch):
    payload = {
        "data": [
            {
                "tm": "2026-09-12 20:00:00",
                "stcd": "REAL_TEST_STCD",
                "pdr": 60,
                "last_modify_time": 1234567890,
                "id": 1,
                "flag": 1,
                "dyp": 12.5,
                "drp": 2.5,
                "intv": 60,
                "wth": "小雨",
            }
        ]
    }
    monkeypatch.setattr("services.real_water_provider._http_get_json", lambda *a, **k: payload)
    provider = _make_provider(
        monkeypatch, CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"})
    )
    record = provider.get_station_data("ST001")
    assert record["official:stcd"] == "REAL_TEST_STCD"
    assert record["rainfall"] == 2.5            # drp → rainfall
    assert record["official:dyp"] == 12.5       # dyp 仅在官方字段中保留，不覆盖 rainfall
    assert record["rainfall_origin"] == "drp"
    assert record["timestamp"].startswith("2026-09-12T20:00:00")  # tm → collection_time
    assert record["official:wth"] == "小雨"      # wth → 天气状态
    assert record["data_quality"] == "good"
    assert record["water_level"] != 2.5
    assert record["water_level"] != 12.5


def test_client_secret_not_transmitted_in_headers(monkeypatch):
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["headers"] = headers
        return {"data": [{"stcd": "REAL_TEST_STCD", "tm": "2026-09-12 10:00:00", "drp": 5}]}

    monkeypatch.setattr("services.real_water_provider._http_get_json", fake_get)
    provider = _make_provider(
        monkeypatch,
        CHENGDU_CLIENT_SECRET="hush-secret-9",
        CHENGDU_STATION_CODES=json.dumps({"ST001": "REAL_TEST_STCD"}),
    )
    provider.get_station_data("ST001")
    header_text = json.dumps(captured["headers"])
    assert "hush-secret-9" not in header_text  # Client Secret 只作为 HMAC key
    assert list(captured["headers"].keys()) == ["X-Client-Id", "X-Timestamp", "X-Nonce", "X-Signature"]


def test_data_source_endpoint_no_secret_leak(monkeypatch):
    monkeypatch.delenv("WATER_DATA_PROVIDER", raising=False)
    monkeypatch.setenv("CHENGDU_CLIENT_ID", "leak-check-client")
    monkeypatch.setenv("CHENGDU_CLIENT_SECRET", "leak-check-secret-value")
    monkeypatch.setenv("CHENGDU_STATION_CODES", "{}")
    resp = client.get("/api/data-source")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "chengdu_open_data"
    assert body["status"]["configured"] is True
    assert body["status"]["signature_ok"] is True
    assert body["status"]["reachable"] is False
    assert body["status"]["tested_stcd"] is False
    text = json.dumps(body, ensure_ascii=False)
    assert "leak-check-secret-value" not in text
    assert "leak-check-client" not in text
    assert "X-Signature" not in text
    assert "X-Timestamp" not in text