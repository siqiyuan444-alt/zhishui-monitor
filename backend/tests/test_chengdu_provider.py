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