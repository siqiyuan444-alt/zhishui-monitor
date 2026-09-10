import os
import sqlite3
import random
from datetime import datetime, timedelta


DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "water_monitor.db")

STATIONS = [
    {"station_id": "ST001", "station_name": "都江堰水文站", "warning_level": 5.0, "latitude": 30.99, "longitude": 103.64},
    {"station_id": "ST002", "station_name": "金堂水文站", "warning_level": 5.5, "latitude": 30.85, "longitude": 104.43},
    {"station_id": "ST003", "station_name": "温江水文站", "warning_level": 4.8, "latitude": 30.70, "longitude": 103.84},
    {"station_id": "ST004", "station_name": "龙泉驿水文站", "warning_level": 6.0, "latitude": 30.56, "longitude": 104.27},
    {"station_id": "ST005", "station_name": "新津水文站", "warning_level": 5.2, "latitude": 30.41, "longitude": 103.81},
]

WATER_RANGES = {
    "ST001": (3.0, 6.0),
    "ST002": (3.5, 6.5),
    "ST003": (2.5, 5.5),
    "ST004": (4.0, 7.0),
    "ST005": (3.2, 6.2),
}


def calculate_status(water_level: float, warning_level: float, rainfall: float) -> str:
    if water_level >= warning_level * 1.1 or rainfall >= 50:
        return "超警"
    if water_level >= warning_level or rainfall >= 30:
        return "警戒"
    if water_level >= warning_level * 0.8 or rainfall >= 15:
        return "注意"
    return "正常"


def get_rainfall_level(rainfall: float) -> str:
    if rainfall >= 50:
        return "暴雨"
    if rainfall >= 30:
        return "大雨"
    if rainfall >= 15:
        return "中雨"
    if rainfall >= 5:
        return "小雨"
    return "无明显降雨"


def generate_rainfall() -> float:
    weights = [
        (0, 5, 50),
        (5, 15, 25),
        (15, 30, 15),
        (30, 50, 7),
        (50, 80, 3),
    ]
    total = sum(w for _, _, w in weights)
    r = random.uniform(0, total)
    cumulative = 0
    for lo, hi, w in weights:
        cumulative += w
        if r <= cumulative:
            return round(random.uniform(lo, hi), 1)
    return 0.0


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


def insert_water_data(station_id: str, station_name: str, water_level: float, warning_level: float, rainfall: float, status: str) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    created_at = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO water_data (station_id, station_name, water_level, warning_level, rainfall, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (station_id, station_name, water_level, warning_level, rainfall, status, created_at),
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
        "INSERT INTO warning_records (station_id, station_name, water_level, warning_level, rainfall, warning_type, warning_message, created_at, is_handled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (station_id, station_name, water_level, warning_level, rainfall, warning_type, warning_message, created_at),
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
    cursor.execute("UPDATE warning_records SET is_handled = 1 WHERE id = ?", (warning_id,))
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
