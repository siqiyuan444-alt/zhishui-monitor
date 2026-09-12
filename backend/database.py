import os
import random
import sqlite3
from datetime import datetime, timedelta

from services import STATIONS, WATER_RANGES
from services.mock_water_provider import (
    calculate_status,
    get_rainfall_level,
    generate_rainfall,
)

DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.environ.get("WATER_MONITOR_DB_PATH") or os.path.join(DB_DIR, "water_monitor.db")


def get_connection():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL UNIQUE,
            station_name TEXT NOT NULL,
            warning_level REAL NOT NULL,
            latitude REAL NOT NULL DEFAULT 0.0,
            longitude REAL NOT NULL DEFAULT 0.0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS water_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL DEFAULT 'ST001',
            station_name TEXT NOT NULL,
            water_level REAL NOT NULL,
            warning_level REAL NOT NULL,
            rainfall REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (station_id) REFERENCES stations(station_id)
        )
    """)

    columns = [row[1] for row in cursor.execute("PRAGMA table_info(water_data)").fetchall()]
    if "station_id" not in columns:
        cursor.execute("ALTER TABLE water_data ADD COLUMN station_id TEXT NOT NULL DEFAULT 'ST001'")
        cursor.execute("UPDATE water_data SET station_id = 'ST001' WHERE station_id IS NULL OR station_id = ''")
    if "rainfall" not in columns:
        cursor.execute("ALTER TABLE water_data ADD COLUMN rainfall REAL NOT NULL DEFAULT 0.0")

    # ── 第13阶段迁移：新增数据来源 / 数据质量 / 采集时间字段（保留旧数据）──
    if "source" not in columns:
        cursor.execute("ALTER TABLE water_data ADD COLUMN source TEXT NOT NULL DEFAULT 'mock'")
    if "data_quality" not in columns:
        cursor.execute("ALTER TABLE water_data ADD COLUMN data_quality TEXT NOT NULL DEFAULT 'valid'")
    if "collected_at" not in columns:
        cursor.execute("ALTER TABLE water_data ADD COLUMN collected_at TEXT NOT NULL DEFAULT ''")
        cursor.execute("UPDATE water_data SET collected_at = created_at WHERE collected_at = '' OR collected_at IS NULL")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collection_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            record_count INTEGER NOT NULL DEFAULT 0,
            valid_count INTEGER NOT NULL DEFAULT 0,
            invalid_count INTEGER NOT NULL DEFAULT 0,
            fallback_count INTEGER NOT NULL DEFAULT 0,
            error_reason TEXT NOT NULL DEFAULT '',
            collected_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS warning_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            station_name TEXT NOT NULL,
            water_level REAL NOT NULL,
            warning_level REAL NOT NULL,
            rainfall REAL NOT NULL DEFAULT 0.0,
            warning_type TEXT NOT NULL,
            warning_message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_handled INTEGER NOT NULL DEFAULT 0
        )
    """)

    # ── 第17阶段迁移：智能预警中心扩展字段（保留旧数据，不删除已有记录）──
    warning_columns = [row[1] for row in cursor.execute("PRAGMA table_info(warning_records)").fetchall()]
    if "alert_level" not in warning_columns:
        cursor.execute("ALTER TABLE warning_records ADD COLUMN alert_level TEXT NOT NULL DEFAULT 'normal'")
    if "title" not in warning_columns:
        cursor.execute("ALTER TABLE warning_records ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    if "status" not in warning_columns:
        cursor.execute("ALTER TABLE warning_records ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
    if "acknowledged_at" not in warning_columns:
        cursor.execute("ALTER TABLE warning_records ADD COLUMN acknowledged_at TEXT NOT NULL DEFAULT ''")
    if "acknowledged_by" not in warning_columns:
        cursor.execute("ALTER TABLE warning_records ADD COLUMN acknowledged_by TEXT NOT NULL DEFAULT ''")
    if "resolved_at" not in warning_columns:
        cursor.execute("ALTER TABLE warning_records ADD COLUMN resolved_at TEXT NOT NULL DEFAULT ''")
    if "resolved_by" not in warning_columns:
        cursor.execute("ALTER TABLE warning_records ADD COLUMN resolved_by TEXT NOT NULL DEFAULT ''")
    # 一次性回填旧记录：从 legacy warning_type / is_handled 推导预警等级与处理状态
    cursor.execute(
        "UPDATE warning_records SET alert_level = CASE warning_type "
        "WHEN '超警预警' THEN 'danger' "
        "WHEN '警戒预警' THEN 'warning' "
        "WHEN '注意预警' THEN 'attention' "
        "ELSE 'normal' END "
        "WHERE alert_level = 'normal' AND warning_type != ''"
    )
    cursor.execute(
        "UPDATE warning_records SET status = 'resolved', resolved_at = created_at "
        "WHERE status = 'pending' AND is_handled = 1"
    )
    cursor.execute(
        "UPDATE warning_records SET title = warning_type WHERE title = '' AND warning_type != ''"
    )

    station_columns = [row[1] for row in cursor.execute("PRAGMA table_info(stations)").fetchall()]
    if "latitude" not in station_columns:
        cursor.execute("ALTER TABLE stations ADD COLUMN latitude REAL NOT NULL DEFAULT 0.0")
        cursor.execute("ALTER TABLE stations ADD COLUMN longitude REAL NOT NULL DEFAULT 0.0")
        for s in STATIONS:
            cursor.execute(
                "UPDATE stations SET latitude = ?, longitude = ? WHERE station_id = ?",
                (s["latitude"], s["longitude"], s["station_id"]),
            )

    for s in STATIONS:
        cursor.execute(
            "UPDATE stations SET station_name = ?, latitude = ?, longitude = ? WHERE station_id = ?",
            (s["station_name"], s["latitude"], s["longitude"], s["station_id"]),
        )

    conn.commit()

    station_count = cursor.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    if station_count == 0:
        _insert_stations(conn)

    water_count = cursor.execute("SELECT COUNT(*) FROM water_data").fetchone()[0]
    if water_count == 0:
        _insert_initial_data(conn)
    else:
        _ensure_all_stations_have_data(conn)

    # 第14阶段：为历史分析提供足够宽的（模拟）历史窗口，幂等且不删除任何数据
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_water_data_station_time ON water_data (station_id, created_at)"
    )
    ensure_history_backfill(conn)

    _init_users(conn)
    _ensure_admin_user(conn, os.environ.get("ADMIN_PASSWORD", ""))

    conn.close()


def _insert_stations(conn):
    cursor = conn.cursor()
    for s in STATIONS:
        cursor.execute(
            "INSERT OR IGNORE INTO stations (station_id, station_name, warning_level, latitude, longitude) VALUES (?, ?, ?, ?, ?)",
            (s["station_id"], s["station_name"], s["warning_level"], s["latitude"], s["longitude"]),
        )
    conn.commit()


def _insert_initial_data(conn):
    cursor = conn.cursor()
    now = datetime.now()

    for station in STATIONS:
        sid = station["station_id"]
        sname = station["station_name"]
        wlevel = station["warning_level"]
        lo, hi = WATER_RANGES[sid]

        records = []
        for i in range(20):
            water_level = round(random.uniform(lo, hi), 2)
            rainfall = generate_rainfall()
            status = calculate_status(water_level, wlevel, rainfall)
            created_at = (now - timedelta(minutes=(20 - i) * 5)).isoformat(timespec="seconds")
            records.append((sid, sname, water_level, wlevel, rainfall, status, created_at))

        cursor.executemany(
            "INSERT INTO water_data (station_id, station_name, water_level, warning_level, rainfall, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            records,
        )
    conn.commit()


def _ensure_all_stations_have_data(conn):
    cursor = conn.cursor()
    now = datetime.now()

    for station in STATIONS:
        sid = station["station_id"]
        count = cursor.execute("SELECT COUNT(*) FROM water_data WHERE station_id = ?", (sid,)).fetchone()[0]
        if count == 0:
            sname = station["station_name"]
            wlevel = station["warning_level"]
            lo, hi = WATER_RANGES[sid]

            records = []
            for i in range(20):
                water_level = round(random.uniform(lo, hi), 2)
                rainfall = generate_rainfall()
                status = calculate_status(water_level, wlevel, rainfall)
                created_at = (now - timedelta(minutes=(20 - i) * 5)).isoformat(timespec="seconds")
                records.append((sid, sname, water_level, wlevel, rainfall, status, created_at))

            cursor.executemany(
                "INSERT INTO water_data (station_id, station_name, water_level, warning_level, rainfall, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                records,
            )
    conn.commit()


def ensure_history_backfill(conn, days: int = None, interval_minutes: int = 30):
    """为历史分析补足模拟历史数据（幂等）。

    当某个水文站的历史样本早于目标窗口起点时，向后逐段生成本地模拟数据，
    使 24 小时 / 7 天范围内的历史趋势分析有数据可查。不删除已有数据。
    目标天数可通过环境变量 HISTORY_BACKFILL_DAYS 调整（默认 7 天）。
    """
    if days is None:
        try:
            days = int(os.environ.get("HISTORY_BACKFILL_DAYS", "7"))
        except (TypeError, ValueError):
            days = 7
    days = max(1, min(days, 30))

    cursor = conn.cursor()
    target_oldest = datetime.now() - timedelta(days=days)

    for station in STATIONS:
        sid = station["station_id"]
        sname = station["station_name"]
        wlevel = station["warning_level"]
        lo, hi = WATER_RANGES[sid]

        row = cursor.execute(
            "SELECT MIN(created_at) AS min_ts FROM water_data WHERE station_id = ?", (sid,)
        ).fetchone()
        min_ts = row["min_ts"]
        if not min_ts:
            continue
        try:
            min_dt = datetime.fromisoformat(min_ts)
        except (ValueError, TypeError):
            continue

        if min_dt <= target_oldest:
            continue

        records = []
        ts = min_dt - timedelta(minutes=interval_minutes)
        while ts > target_oldest:
            water_level = round(random.uniform(lo, hi), 2)
            rainfall = generate_rainfall()
            status = calculate_status(water_level, wlevel, rainfall)
            ts_text = ts.isoformat(timespec="seconds")
            records.append((sid, sname, water_level, wlevel, rainfall, status, ts_text, "mock", "valid", ts_text))
            ts -= timedelta(minutes=interval_minutes)

        if records:
            cursor.executemany(
                "INSERT INTO water_data (station_id, station_name, water_level, warning_level, rainfall, status, created_at, source, data_quality, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                records,
            )
    conn.commit()


def get_stations() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT station_id, station_name, warning_level, latitude, longitude FROM stations ORDER BY station_id")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_station_by_id(station_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT station_id, station_name, warning_level, latitude, longitude FROM stations WHERE station_id = ?", (station_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def insert_water_data(station_id: str, station_name: str, water_level: float, warning_level: float, rainfall: float, status: str,
                      source: str = "mock", data_quality: str = "valid",
                      created_at: str = None, collected_at: str = None) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    if not created_at:
        created_at = datetime.now().isoformat(timespec="seconds")
    if not collected_at:
        collected_at = created_at
    cursor.execute(
        "INSERT INTO water_data (station_id, station_name, water_level, warning_level, rainfall, status, created_at, source, data_quality, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (station_id, station_name, water_level, warning_level, rainfall, status, created_at, source, data_quality, collected_at),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def get_water_history(station_id: str = "ST001", limit: int = 20) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, station_id, station_name, water_level, warning_level, rainfall, status, created_at FROM water_data WHERE station_id = ? ORDER BY created_at ASC LIMIT ?",
        (station_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_history_since(station_id: str, since_iso: str, limit: int = None) -> list:
    """返回某个水文站指定时间段之后的所有历史记录（时间正序）。"""
    conn = get_connection()
    cursor = conn.cursor()
    sql = (
        "SELECT id, station_id, station_name, water_level, warning_level, rainfall, status, created_at, source, data_quality "
        "FROM water_data WHERE station_id = ? AND created_at >= ? ORDER BY created_at ASC"
    )
    params = [station_id, since_iso]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_station_stats(station_id: str, since_iso: str) -> dict:
    """统计指定时间段内某站的水位/降雨聚合值。"""
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT COUNT(*) AS n, "
        "MAX(water_level) AS mx, MIN(water_level) AS mn, AVG(water_level) AS av, "
        "MAX(rainfall) AS mxr, AVG(rainfall) AS avr, COALESCE(SUM(rainfall), 0) AS tot "
        "FROM water_data WHERE station_id = ? AND created_at >= ?",
        (station_id, since_iso),
    ).fetchone()
    conn.close()
    return dict(row)


def count_warnings_since(station_id: str, since_iso: str) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT COUNT(*) AS n FROM warning_records WHERE station_id = ? AND created_at >= ?",
        (station_id, since_iso),
    ).fetchone()
    conn.close()
    return row["n"]


def get_all_history_since(since_iso: str) -> list:
    """返回指定时间段之后所有水文站的历史记录（时间正序，含站名等）。"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT station_id, station_name, water_level, warning_level, rainfall, created_at "
        "FROM water_data WHERE created_at >= ? ORDER BY created_at ASC",
        (since_iso,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def count_all_warnings_since(since_iso: str) -> dict:
    """返回指定时间段之后各站预警数量 {station_id: count}。"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT station_id, COUNT(*) AS n FROM warning_records WHERE created_at >= ? GROUP BY station_id",
        (since_iso,),
    )
    rows = cursor.fetchall()
    conn.close()
    return {row["station_id"]: row["n"] for row in rows}


def get_all_latest_water_data() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT w.id, w.station_id, w.station_name, w.water_level, w.warning_level, w.rainfall, w.status, w.created_at
        FROM water_data w
        INNER JOIN (
            SELECT station_id, MAX(id) as max_id FROM water_data GROUP BY station_id
        ) latest ON w.id = latest.max_id
        ORDER BY w.station_id
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_rainfall_summary(station_id: str) -> dict:
    conn = get_connection()
    cursor = conn.cursor()

    station = get_station_by_id(station_id)
    station_name = station["station_name"] if station else station_id

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    h24_ago = now - timedelta(hours=24)

    row = cursor.execute(
        "SELECT COALESCE(SUM(rainfall), 0) as total FROM water_data WHERE station_id = ? AND created_at >= ?",
        (station_id, today_start.isoformat(timespec="seconds")),
    ).fetchone()
    today_rainfall = round(row["total"], 1)

    row = cursor.execute(
        "SELECT COALESCE(SUM(rainfall), 0) as total FROM water_data WHERE station_id = ? AND created_at >= ?",
        (station_id, h24_ago.isoformat(timespec="seconds")),
    ).fetchone()
    last_24h_rainfall = round(row["total"], 1)

    row = cursor.execute(
        "SELECT COALESCE(MAX(rainfall), 0) as max_rain FROM water_data WHERE station_id = ?",
        (station_id,),
    ).fetchone()
    max_rainfall = round(row["max_rain"], 1)

    conn.close()

    rainfall_level = get_rainfall_level(last_24h_rainfall)

    return {
        "station_id": station_id,
        "station_name": station_name,
        "today_rainfall": today_rainfall,
        "last_24h_rainfall": last_24h_rainfall,
        "max_rainfall": max_rainfall,
        "rainfall_level": rainfall_level,
    }


WARNING_TYPE_MAP = {
    "注意": "注意预警",
    "警戒": "警戒预警",
    "超警": "超警预警",
}

WARNING_MESSAGE_MAP = {
    "注意": "{station_name}当前水位或降雨量达到注意阈值，请加强监测。",
    "警戒": "{station_name}当前水位或降雨量达到警戒阈值，请及时关注。",
    "超警": "{station_name}当前水位或降雨量达到超警阈值，请立即采取相应措施。",
}


def create_warning(station_id: str, station_name: str, water_level: float, warning_level: float, rainfall: float, status: str) -> int | None:
    if status not in WARNING_TYPE_MAP:
        return None

    warning_type = WARNING_TYPE_MAP[status]
    warning_message = WARNING_MESSAGE_MAP[status].format(station_name=station_name)
    alert_level = {"注意": "attention", "警戒": "warning", "超警": "danger"}.get(status, "normal")

    conn = get_connection()
    cursor = conn.cursor()

    five_min_ago = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    dup = cursor.execute(
        "SELECT id FROM warning_records WHERE station_id = ? AND warning_type = ? AND created_at >= ? AND is_handled = 0",
        (station_id, warning_type, five_min_ago),
    ).fetchone()

    if dup:
        conn.close()
        return None

    created_at = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO warning_records (station_id, station_name, water_level, warning_level, rainfall, warning_type, warning_message, created_at, is_handled, alert_level, title, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'pending')",
        (station_id, station_name, water_level, warning_level, rainfall, warning_type, warning_message, created_at, alert_level, warning_type),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def get_warnings(limit: int = 50) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, station_id, station_name, water_level, warning_level, rainfall, warning_type, warning_message, created_at, is_handled FROM warning_records ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_active_warnings() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, station_id, station_name, water_level, warning_level, rainfall, warning_type, warning_message, created_at, is_handled FROM warning_records WHERE is_handled = 0 ORDER BY created_at DESC"
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_warnings_summary() -> dict:
    conn = get_connection()
    cursor = conn.cursor()

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    row = cursor.execute("SELECT COUNT(*) as cnt FROM warning_records WHERE created_at >= ?", (today_start,)).fetchone()
    today_total = row["cnt"]

    row = cursor.execute("SELECT COUNT(*) as cnt FROM warning_records WHERE created_at >= ? AND warning_type = '注意预警'", (today_start,)).fetchone()
    attention = row["cnt"]

    row = cursor.execute("SELECT COUNT(*) as cnt FROM warning_records WHERE created_at >= ? AND warning_type = '警戒预警'", (today_start,)).fetchone()
    warning = row["cnt"]

    row = cursor.execute("SELECT COUNT(*) as cnt FROM warning_records WHERE created_at >= ? AND warning_type = '超警预警'", (today_start,)).fetchone()
    critical = row["cnt"]

    row = cursor.execute("SELECT COUNT(*) as cnt FROM warning_records WHERE is_handled = 0").fetchone()
    active = row["cnt"]

    conn.close()

    return {
        "today_total": today_total,
        "attention": attention,
        "warning": warning,
        "critical": critical,
        "active": active,
    }


def handle_warning(warning_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM warning_records WHERE id = ?", (warning_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    resolved_at = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "UPDATE warning_records SET is_handled = 1, status = 'resolved', resolved_at = ? WHERE id = ?",
        (resolved_at, warning_id),
    )
    conn.commit()
    conn.close()
    return True


def get_latest_warning(station_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, station_id, station_name, water_level, warning_level, rainfall, warning_type, warning_message, created_at, is_handled FROM warning_records WHERE station_id = ? ORDER BY created_at DESC LIMIT 1",
        (station_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


# ──────────────────────────── 第17阶段：智能预警中心 ────────────────────────────


def has_active_alert(station_id: str, alert_level: str) -> bool:
    """同站点同等级是否存在未解除（pending / acknowledged）的预警。"""
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT id FROM warning_records WHERE station_id = ? AND alert_level = ? AND status IN ('pending', 'acknowledged') LIMIT 1",
        (station_id, alert_level),
    ).fetchone()
    conn.close()
    return row is not None


def create_alert(station_id: str, station_name: str, water_level: float,
                 warning_level_value: float, rainfall: float, alert_level: str,
                 warning_type: str, title: str, message: str) -> int | None:
    conn = get_connection()
    cursor = conn.cursor()
    created_at = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO warning_records (station_id, station_name, water_level, warning_level, rainfall, warning_type, warning_message, created_at, is_handled, alert_level, title, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'pending')",
        (station_id, station_name, water_level, warning_level_value, rainfall, warning_type, message, created_at, alert_level, title),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


_ALERT_SELECT = (
    "SELECT id, station_id, station_name, water_level, warning_level, rainfall, "
    "warning_type, warning_message, created_at, is_handled, "
    "alert_level, title, status, acknowledged_at, acknowledged_by, resolved_at, resolved_by "
    "FROM warning_records WHERE 1=1"
)


def _map_alert_row(row) -> dict:
    return {
        "id": row["id"],
        "station_id": row["station_id"],
        "station_name": row["station_name"],
        "warning_level": row["alert_level"],
        "warning_type": row["warning_type"],
        "title": row["title"],
        "message": row["warning_message"],
        "water_level": row["water_level"],
        "warning_level_value": row["warning_level"],
        "rainfall": row["rainfall"],
        "status": row["status"],
        "created_at": row["created_at"],
        "acknowledged_at": row["acknowledged_at"],
        "acknowledged_by": row["acknowledged_by"],
        "resolved_at": row["resolved_at"],
        "resolved_by": row["resolved_by"],
    }


def _alert_query_params(station_id: str = None, alert_level: str = None,
                        status: str = None, since_iso: str = None) -> tuple:
    sql = _ALERT_SELECT
    params: list = []
    if station_id:
        sql += " AND station_id = ?"
        params.append(station_id)
    if alert_level:
        sql += " AND alert_level = ?"
        params.append(alert_level)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if since_iso:
        sql += " AND created_at >= ?"
        params.append(since_iso)
    return sql, params


def get_alerts(station_id: str = None, alert_level: str = None, status: str = None,
               since_iso: str = None, limit: int = 50, offset: int = 0) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    sql, params = _alert_query_params(station_id, alert_level, status, since_iso)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params += [int(limit), int(offset)]
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return [_map_alert_row(row) for row in rows]


def count_alerts(station_id: str = None, alert_level: str = None, status: str = None,
                 since_iso: str = None) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    sql, params = _alert_query_params(station_id, alert_level, status, since_iso)
    sql = "SELECT COUNT(*) AS n FROM (" + sql + ")"
    row = cursor.execute(sql, params).fetchone()
    conn.close()
    return row["n"]


def get_alert_summary() -> dict:
    """返回全量预警汇总（总数 / 处理状态 / 等级分布）。"""
    conn = get_connection()
    cursor = conn.cursor()

    def _count(where: str, params: tuple = ()) -> int:
        return cursor.execute(f"SELECT COUNT(*) AS n FROM warning_records WHERE {where}", params).fetchone()["n"]

    summary = {
        "total": _count("1=1"),
        "pending": _count("status = 'pending'"),
        "acknowledged": _count("status = 'acknowledged'"),
        "resolved": _count("status = 'resolved'"),
        "normal": _count("alert_level = 'normal'"),
        "attention": _count("alert_level = 'attention'"),
        "warning": _count("alert_level = 'warning'"),
        "danger": _count("alert_level = 'danger'"),
    }
    conn.close()
    return summary


def acknowledge_alert(alert_id: int, username: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute("SELECT id FROM warning_records WHERE id = ?", (alert_id,)).fetchone()
    if not row:
        conn.close()
        return False
    now = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "UPDATE warning_records SET status = 'acknowledged', acknowledged_at = ?, acknowledged_by = ? WHERE id = ?",
        (now, username, alert_id),
    )
    conn.commit()
    conn.close()
    return True


def resolve_alert(alert_id: int, username: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute("SELECT id FROM warning_records WHERE id = ?", (alert_id,)).fetchone()
    if not row:
        conn.close()
        return False
    now = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "UPDATE warning_records SET status = 'resolved', resolved_at = ?, resolved_by = ?, is_handled = 1 WHERE id = ?",
        (now, username, alert_id),
    )
    conn.commit()
    conn.close()
    return True


# ──────────────────────────── 第16阶段：数据报表导出 ────────────────────────────


def get_report_data(station_id: str = None, since_iso: str = None, limit: int = None) -> list:
    """返回报表所需记录（时间倒序）。station_id 为 None 时返回全部站点。"""
    conn = get_connection()
    cursor = conn.cursor()
    sql = (
        "SELECT station_id, station_name, created_at AS timestamp, "
        "water_level, warning_level, rainfall, status, source, data_quality, "
        "COALESCE(NULLIF(collected_at, ''), created_at) AS collected_at "
        "FROM water_data WHERE 1=1"
    )
    params: list = []
    if station_id:
        sql += " AND station_id = ?"
        params.append(station_id)
    if since_iso:
        sql += " AND created_at >= ?"
        params.append(since_iso)
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_report_stats(station_id: str = None, since_iso: str = None) -> dict:
    """返回报表统计。station_id 为 None 时统计全部站点。"""
    conn = get_connection()
    cursor = conn.cursor()

    sql = (
        "SELECT COUNT(*) AS n, "
        "MAX(water_level) AS mx, MIN(water_level) AS mn, AVG(water_level) AS av, "
        "MAX(rainfall) AS mxr, AVG(rainfall) AS avr, COALESCE(SUM(rainfall), 0) AS tot "
        "FROM water_data WHERE 1=1"
    )
    params: list = []
    if station_id:
        sql += " AND station_id = ?"
        params.append(station_id)
    if since_iso:
        sql += " AND created_at >= ?"
        params.append(since_iso)
    row = cursor.execute(sql, params).fetchone()

    ws_sql = "SELECT COUNT(*) AS n FROM warning_records WHERE 1=1"
    ws_params: list = []
    if station_id:
        ws_sql += " AND station_id = ?"
        ws_params.append(station_id)
    if since_iso:
        ws_sql += " AND created_at >= ?"
        ws_params.append(since_iso)
    ws_row = cursor.execute(ws_sql, ws_params).fetchone()

    conn.close()
    result = dict(row)
    result["warning_count"] = ws_row["n"]
    return result


# ──────────────────────────── 第13阶段：数据质量统计 / 采集日志 ────────────────────────────


def get_data_quality_stats() -> dict:
    conn = get_connection()
    cursor = conn.cursor()

    row = cursor.execute("SELECT COUNT(*) as cnt FROM water_data").fetchone()
    total = row["cnt"]

    def count_for(quality: str) -> int:
        return cursor.execute(
            "SELECT COUNT(*) as cnt FROM water_data WHERE data_quality = ?", (quality,)
        ).fetchone()["cnt"]

    latest = cursor.execute("SELECT MAX(collected_at) as ts FROM water_data").fetchone()["ts"]

    conn.close()
    return {
        "total": total,
        "valid": count_for("valid"),
        "invalid": count_for("invalid"),
        "fallback": count_for("fallback"),
        "latest_collection_time": latest or "",
    }


def log_collection(provider: str, status: str, record_count: int = 0, valid_count: int = 0,
                   invalid_count: int = 0, fallback_count: int = 0,
                   error_reason: str = "", collected_at: str = None) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    if not collected_at:
        collected_at = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO collection_logs (provider, status, record_count, valid_count, invalid_count, fallback_count, error_reason, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (provider, status, record_count, valid_count, invalid_count, fallback_count, error_reason, collected_at),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def get_collection_logs(limit: int = 20) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, provider, status, record_count, valid_count, invalid_count, fallback_count, error_reason, collected_at FROM collection_logs ORDER BY collected_at DESC LIMIT ?",
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def create_user(username: str, password_hash: str, role: str = "user") -> int:
    conn = get_connection()
    cursor = conn.cursor()
    created_at = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
        (username, password_hash, role, created_at),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def get_user_by_username(username: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, password_hash, role, is_active, created_at FROM users WHERE username = ?",
        (username,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, role, is_active, created_at FROM users WHERE id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, role, is_active, created_at FROM users ORDER BY id"
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_user(user_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True


def update_user_role(user_id: int, role: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    cursor.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    conn.close()
    return True


def _init_users(conn):
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


def _ensure_admin_user(conn, admin_password: str):
    from passlib.hash import argon2

    cursor = conn.cursor()
    admin = cursor.execute(
        "SELECT id FROM users WHERE username = 'admin'"
    ).fetchone()

    if not admin and admin_password:
        password_hash = argon2.hash(admin_password)
        created_at = datetime.now().isoformat(timespec="seconds")
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            ("admin", password_hash, "admin", created_at),
        )
        conn.commit()


init_db()
