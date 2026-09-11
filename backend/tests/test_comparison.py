"""第15阶段：多站综合对比分析测试。"""

from fastapi.testclient import TestClient

import main
import database
from services.data_analysis import risk_level_for

client = TestClient(main.app)

RISK_ORDER = {"danger": 3, "warning": 2, "attention": 1, "normal": 0}


def test_comparison_api_200():
    resp = client.get("/api/comparison?hours=24")
    assert resp.status_code == 200
    d = resp.json()
    assert d["hours"] == 24
    assert isinstance(d["count"], int)
    assert d["count"] == 5
    assert d["sufficient"] is True
    assert len(d["stations"]) == 5


def test_comparison_24h_points():
    d = client.get("/api/comparison?hours=24").json()
    for s in d["stations"]:
        assert s["station_id"].startswith("ST")
        assert isinstance(s["current_water_level"], (int, float))
        assert isinstance(s["warning_level"], (int, float))
        assert s["water_level_trend"] in ("rising", "falling", "stable")
        assert s["max_water_level"] is not None
        assert s["min_water_level"] is not None
        assert s["average_water_level"] is not None
        assert isinstance(s["total_rainfall"], (int, float))
        assert isinstance(s["warning_count"], int)
        assert s["data_points"] >= 1
    assert len(d["series"]["timestamps"]) >= 1
    assert set(d["series"]["levels"].keys()) == set(s["station_id"] for s in d["stations"])


def test_comparison_7days():
    resp = client.get("/api/comparison?hours=168")
    assert resp.status_code == 200
    d = resp.json()
    assert d["hours"] == 168
    assert d["count"] == 5
    assert len(d["series"]["timestamps"]) >= 10


def test_comparison_invalid_hours_422():
    assert client.get("/api/comparison?hours=0").status_code == 422
    assert client.get("/api/comparison?hours=abc").status_code == 422
    assert client.get("/api/comparison?hours=99999").status_code == 422


def test_comparison_unknown_station_not_crash():
    database.insert_water_data(
        station_id="ST999",
        station_name="未知站",
        water_level=4.2,
        warning_level=5.0,
        rainfall=10.0,
        status="正常",
    )
    try:
        resp = client.get("/api/comparison?hours=24")
        assert resp.status_code == 200
        d = resp.json()
        assert d["count"] == 5
        assert d["sufficient"] is True
    finally:
        conn = database.get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM water_data WHERE station_id = 'ST999'")
        conn.commit()
        conn.close()


def test_comparison_single_station_insufficient():
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM water_data WHERE station_id = 'ST005'")
    conn.commit()
    conn.close()
    try:
        resp = client.get("/api/comparison?hours=24")
        assert resp.status_code == 200
        d = resp.json()
        target = next(s for s in d["stations"] if s["station_id"] == "ST005")
        assert target["sufficient"] is False
        assert target["current_water_level"] is None
        assert target["risk_level"] is None
        assert d["sufficient"] is True
    finally:
        database._insert_initial_data(database.get_connection())
        conn = database.get_connection()
        database.ensure_history_backfill(conn)
        conn.close()


def test_comparison_overall_insufficient():
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM water_data")
    conn.commit()
    conn.close()
    try:
        resp = client.get("/api/comparison?hours=24")
        assert resp.status_code == 200
        d = resp.json()
        assert d["sufficient"] is False
        assert d["highest_water_level_station"] is None
        assert d["fastest_rising_station"] is None
        assert d["highest_rainfall_station"] is None
        assert d["highest_risk_station"] is None
        assert all(s["sufficient"] is False for s in d["stations"])
    finally:
        database._insert_initial_data(database.get_connection())
        conn = database.get_connection()
        database.ensure_history_backfill(conn)
        conn.close()


def test_comparison_risk_ranking_order():
    d = client.get("/api/comparison?hours=24").json()
    ranking = d["risk_ranking"]
    assert len(ranking) == 5
    for prev, cur in zip(ranking, ranking[1:]):
        prev_order = RISK_ORDER.get(prev["risk_level"] or "normal", -1)
        cur_order = RISK_ORDER.get(cur["risk_level"] or "normal", -1)
        assert prev_order >= cur_order
        if prev_order == cur_order:
            prev_ratio = prev.get("risk_ratio") or 0.0
            cur_ratio = cur.get("risk_ratio") or 0.0
            assert prev_ratio >= cur_ratio


def test_comparison_risk_level_consistent():
    d = client.get("/api/comparison?hours=24").json()
    for s in d["stations"]:
        if not s["sufficient"]:
            continue
        expected = risk_level_for(s["current_water_level"], s["warning_level"])
        assert s["risk_level"] == expected


def test_comparison_risk_ratio():
    d = client.get("/api/comparison?hours=24").json()
    for s in d["stations"]:
        if not s["sufficient"]:
            continue
        ratio = round(s["current_water_level"] / s["warning_level"], 3)
        assert s["risk_ratio"] == ratio


def test_comparison_highest_water_level_station():
    d = client.get("/api/comparison?hours=24").json()
    expect = max(
        (s for s in d["stations"] if s["sufficient"]),
        key=lambda s: s["current_water_level"],
    )
    top = d["highest_water_level_station"]
    assert top["station_id"] == expect["station_id"]
    assert top["current_water_level"] == expect["current_water_level"]


def test_comparison_highest_rainfall_station():
    d = client.get("/api/comparison?hours=24").json()
    expect = max(
        (s for s in d["stations"] if s["sufficient"]),
        key=lambda s: s["total_rainfall"],
    )
    top = d["highest_rainfall_station"]
    assert top["station_id"] == expect["station_id"]
    assert top["total_rainfall"] == expect["total_rainfall"]


def test_comparison_fastest_rising_station():
    d = client.get("/api/comparison?hours=24").json()
    expect = max(
        (s for s in d["stations"] if s["sufficient"]),
        key=lambda s: s["rate_per_hour"],
    )
    top = d["fastest_rising_station"]
    assert top["station_id"] == expect["station_id"]
    assert top["rate_per_hour"] == expect["rate_per_hour"]


def test_comparison_highest_risk_station():
    d = client.get("/api/comparison?hours=24").json()
    top = d["highest_risk_station"]
    assert top["risk_level"] is not None
    for s in d["stations"]:
        if not s["sufficient"] or s["risk_level"] is None:
            continue
        assert RISK_ORDER.get(top["risk_level"]) >= RISK_ORDER.get(s["risk_level"])
    assert d["risk_ranking"][0]["station_id"] == top["station_id"]


def test_comparison_does_not_affect_existing_apis():
    assert client.get("/api/stations").status_code == 200
    assert client.get("/api/water-data?station_id=ST001").status_code == 200
    assert client.get("/api/water-data-all").status_code == 200
    assert client.get("/api/history?station_id=ST001&hours=24").status_code == 200
    assert client.get("/api/trend?station_id=ST001&hours=24").status_code == 200
    assert client.get("/api/statistics?station_id=ST001&hours=24").status_code == 200
    assert client.get("/api/forecast?station_id=ST001&hours=24").status_code == 200
    assert client.get("/api/warnings?limit=5").status_code == 200
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()