"""第14阶段：历史数据分析与趋势预测测试。"""

from fastapi.testclient import TestClient

import main
import database

client = TestClient(main.app)


def test_history_api_200():
    resp = client.get("/api/history?station_id=ST001&hours=24")
    assert resp.status_code == 200
    d = resp.json()
    assert d["station_id"] == "ST001"
    assert d["hours"] == 24
    assert isinstance(d["data"], list)
    assert d["count"] >= 1
    for r in d["data"]:
        assert "timestamp" in r
        assert "water_level" in r
        assert "warning_level" in r
        assert "rainfall" in r
        assert "status" in r
        assert "source" in r
        assert "data_quality" in r


def test_history_api_7days():
    resp = client.get("/api/history?station_id=ST002&hours=168")
    assert resp.status_code == 200
    assert resp.json()["hours"] == 168
    assert resp.json()["count"] >= 10


def test_history_invalid_station_404():
    resp = client.get("/api/history?station_id=NOPE&hours=24")
    assert resp.status_code == 404


def test_history_invalid_hours_422():
    resp = client.get("/api/history?station_id=ST001&hours=0")
    assert resp.status_code == 422
    resp2 = client.get("/api/history?station_id=ST001&hours=abc")
    assert resp2.status_code == 422


def test_trend_api_200():
    resp = client.get("/api/trend?station_id=ST001&hours=24")
    assert resp.status_code == 200
    d = resp.json()
    assert d["sufficient"] is True
    assert d["trend"] in ("rising", "falling", "stable")
    assert d["current_water_level"] is not None
    assert d["previous_water_level"] is not None
    assert d["change"] is not None


def test_trend_invalid_station_404():
    assert client.get("/api/trend?station_id=NOPE&hours=24").status_code == 404


def test_trend_invalid_hours_422():
    assert client.get("/api/trend?station_id=ST001&hours=0").status_code == 422


def test_trend_data_insufficient():
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM water_data WHERE station_id='ST005' "
        "AND id NOT IN (SELECT MIN(id) FROM water_data WHERE station_id='ST005')"
    )
    conn.commit()
    conn.close()
    resp = client.get("/api/trend?station_id=ST005&hours=24")
    d = resp.json()
    assert d["sufficient"] is False
    assert "历史数据不足" in d["message"]


def test_statistics_api_200():
    resp = client.get("/api/statistics?station_id=ST001&hours=24")
    assert resp.status_code == 200
    d = resp.json()
    assert d["sufficient"] is True
    assert d["data_points"] >= 1
    assert isinstance(d["max_water_level"], (int, float))
    assert isinstance(d["min_water_level"], (int, float))
    assert isinstance(d["avg_water_level"], (int, float))
    assert isinstance(d["max_rainfall"], (int, float))
    assert isinstance(d["avg_rainfall"], (int, float))
    assert isinstance(d["total_rainfall"], (int, float))
    assert isinstance(d["warning_count"], int)
    assert d["max_water_level"] >= d["min_water_level"]


def test_statistics_invalid_station_404():
    assert client.get("/api/statistics?station_id=NOPE&hours=24").status_code == 404


def test_statistics_invalid_hours_422():
    assert client.get("/api/statistics?station_id=ST001&hours=abc").status_code == 422


def test_statistics_hours_too_large_422():
    assert client.get("/api/statistics?station_id=ST001&hours=99999").status_code == 422


def test_forecast_api_200():
    resp = client.get("/api/forecast?station_id=ST001&hours=24")
    assert resp.status_code == 200
    d = resp.json()
    assert d["sufficient"] is True
    assert d["is_forecast"] is True
    assert isinstance(d["current_water_level"], (int, float))
    assert isinstance(d["predicted_water_level"], (int, float))
    assert d["predicted_water_level"] >= 0
    assert d["trend"] in ("rising", "falling", "stable")
    assert d["risk_level"] in ("normal", "attention", "warning", "danger")
    assert "趋势预测" in d["message"] or "非真实" in d["message"]


def test_forecast_insufficient():
    resp = client.get("/api/forecast?station_id=ST005&hours=24")
    d = resp.json()
    assert d["sufficient"] is False
    assert "历史数据不足" in d["message"]
    assert d["is_forecast"] is True


def test_forecast_invalid_station_404():
    assert client.get("/api/forecast?station_id=NOPE&hours=24").status_code == 404


def test_forecast_invalid_hours_422():
    assert client.get("/api/forecast?station_id=ST001&hours=0").status_code == 422


def test_existing_api_stations_unchanged():
    resp = client.get("/api/stations")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 5


def test_existing_api_water_data_unchanged():
    resp = client.get("/api/water-data?station_id=ST001")
    assert resp.status_code == 200
    assert "water_level" in resp.json()


def test_existing_api_warnings_unchanged():
    resp = client.get("/api/warnings?limit=5")
    assert resp.status_code == 200


def test_existing_api_auth_unchanged():
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()