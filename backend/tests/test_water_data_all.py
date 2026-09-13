import os
import sqlite3
import sys

import pytest

import database as db

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# 旧实现（改动前）使用的查询，用于等价性对照
PREVIOUS_QUERY_SQL = """
    SELECT w.id, w.station_id, w.station_name, w.water_level, w.warning_level,
           w.rainfall, w.status, w.source, w.data_quality, w.created_at
    FROM water_data w
    INNER JOIN (
        SELECT station_id, MAX(id) as max_id FROM water_data GROUP BY station_id
    ) latest ON w.id = latest.max_id
    ORDER BY w.station_id
"""

EXPECTED_KEYS = {
    "id", "station_id", "station_name", "water_level", "warning_level",
    "rainfall", "status", "source", "data_quality", "created_at",
}


def _make_iso_connection(db_file: str):
    def _conn():
        conn = sqlite3.connect(db_file, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    return _conn


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "iso_water.db")
    monkeypatch.setattr(db, "get_connection", _make_iso_connection(db_file))
    db.init_db()
    conn = db.get_connection()
    conn.execute("DELETE FROM water_data")
    conn.commit()
    conn.close()
    return db_file


def _insert(rows):
    conn = db.get_connection()
    conn.executemany(
        "INSERT INTO water_data (station_id, station_name, water_level, warning_level, rainfall, status, created_at, source, data_quality, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_empty_db_returns_empty_list(isolated_db):
    assert db.get_all_latest_water_data() == []


def test_single_station_returns_only_latest(isolated_db):
    _insert([
        ("Z9", "站点Z9", 4.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
        ("Z9", "站点Z9", 4.1, 4.5, 0.0, "正常", "2026-01-01T01:00:00", "mock", "valid", ""),
        ("Z9", "站点Z9", 4.2, 4.5, 0.0, "正常", "2026-01-01T02:00:00", "mock", "valid", ""),
    ])
    result = db.get_all_latest_water_data()
    assert len(result) == 1
    assert set(result[0].keys()) == EXPECTED_KEYS
    assert result[0]["station_id"] == "Z9"
    assert result[0]["water_level"] == 4.2


def test_multiple_stations_latest_per_station_and_order(isolated_db):
    _insert([
        ("Z2", "站点Z2", 1.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
        ("Z1", "站点Z1", 2.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
        ("Z2", "站点Z2", 3.0, 4.5, 0.0, "正常", "2026-01-01T01:00:00", "mock", "valid", ""),
        ("Z1", "站点Z1", 4.0, 4.5, 0.0, "正常", "2026-01-01T01:00:00", "mock", "valid", ""),
    ])
    result = db.get_all_latest_water_data()
    assert [r["station_id"] for r in result] == ["Z1", "Z2"]
    assert {r["station_id"]: r["water_level"] for r in result} == {"Z1": 4.0, "Z2": 3.0}


def test_matches_previous_join_query(isolated_db):
    _insert([
        ("Z2", "站点Z2", 1.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
        ("Z1", "站点Z1", 2.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
        ("Z2", "站点Z2", 3.0, 4.5, 0.0, "正常", "2026-01-01T01:00:00", "mock", "valid", ""),
        ("Z1", "站点Z1", 4.0, 4.5, 0.0, "正常", "2026-01-01T01:00:00", "mock", "valid", ""),
        ("Z3", "站点Z3", 5.0, 4.5, 0.0, "正常", "2026-01-01T02:00:00", "mock", "valid", ""),
    ])
    new = db.get_all_latest_water_data()
    conn = db.get_connection()
    rows = conn.execute(PREVIOUS_QUERY_SQL).fetchall()
    conn.close()
    old = [dict(r) for r in rows]
    assert new == old
    assert [r["station_id"] for r in new] == ["Z1", "Z2", "Z3"]


def test_older_created_at_higher_id_still_wins(isolated_db):
    # 首条 insert 为较新时间（id 较小），第二条为较早时间（id 较大），
    # 与历史回填场景一致：最新语义仍是 MAX(id) 所在行。
    _insert([
        ("Z5", "站点Z5", 1.5, 4.5, 0.0, "正常", "2026-02-01T00:00:00", "mock", "valid", ""),
        ("Z5", "站点Z5", 9.9, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
    ])
    result = db.get_all_latest_water_data()
    assert len(result) == 1
    assert result[0]["water_level"] == 9.9


def test_insert_then_fetch_returns_new_latest(isolated_db):
    _insert([
        ("Z3", "站点Z3", 1.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
    ])
    first = db.get_all_latest_water_data()
    assert len(first) == 1 and first[0]["water_level"] == 1.0

    _insert([
        ("Z3", "站点Z3", 2.0, 4.5, 0.0, "正常", "2026-01-01T01:00:00", "mock", "valid", ""),
    ])
    second = db.get_all_latest_water_data()
    assert len(second) == 1 and second[0]["water_level"] == 2.0


def test_station_order_independent_of_insert_order(isolated_db):
    _insert([
        ("Zx", "站点Zx", 1.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
        ("Za", "站点Za", 1.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
        ("Zm", "站点Zm", 1.0, 4.5, 0.0, "正常", "2026-01-01T00:00:00", "mock", "valid", ""),
    ])
    result = db.get_all_latest_water_data()
    assert [r["station_id"] for r in result] == ["Za", "Zm", "Zx"]