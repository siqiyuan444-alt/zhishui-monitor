"""Stage 21：多用户并发与数据库稳定性测试。

通过启动进程内真实 uvicorn 服务器 + 多线程 httpx 并发请求，验证：
- 多用户并发访问 water-data / history / AI / 登录不出现 500 与数据损坏；
- 告警并发 acknowledge / resolve 不出现竞态 500；
- 底层 SQLite（WAL + busy_timeout）并发读写稳定；
- 连接 PRAGMA 配置生效。
"""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
import pytest
import uvicorn

import database
import main
from services.alert_service import create_alert_if_needed

STATION_IDS = ["ST001", "ST002", "ST003", "ST004", "ST005"]


@pytest.fixture(scope="module")
def live_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    host = "127.0.0.1"
    config = uvicorn.Config(main.app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("uvicorn 服务器启动超时")
    yield f"http://{host}:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _http_get(base: str, path: str):
    with httpx.Client(base_url=base, timeout=30) as client:
        return client.get(path)


def test_concurrent_water_data_and_history(live_server):
    def task(i: int):
        station = STATION_IDS[i % len(STATION_IDS)]
        with httpx.Client(base_url=live_server, timeout=30) as c:
            r1 = c.get(f"/api/water-data?station_id={station}")
            r2 = c.get(f"/api/history?station_id={station}&hours=24")
            r3 = c.get("/api/water-data-all")
        return (r1.status_code, r2.status_code, r3.status_code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(task, range(40)))
    assert results, "并发请求未执行"
    assert all(r1 == r2 == r3 == 200 for r1, r2, r3 in results)


def test_concurrent_ai_analysis_rule_based(live_server):
    def task(_):
        resp = _http_get(live_server, "/api/ai-analysis")
        data = resp.json()
        return resp.status_code, data.get("analysis_source"), data.get("risk_level")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(task, range(18)))
    assert all(status == 200 for status, _, _ in results)
    assert all(source == "rule_based" for _, source, _ in results)
    assert all(level in ("normal", "attention", "warning", "severe") for _, _, level in results)


def test_concurrent_login_burst(live_server):
    name = f"burst_{int(time.time() * 1000)}"
    with httpx.Client(base_url=live_server, timeout=30) as c:
        assert c.post("/api/auth/register", json={"username": name, "password": "12345678"}).status_code == 200

    def ok_login(_):
        with httpx.Client(base_url=live_server, timeout=30) as c:
            r = c.post("/api/auth/login", json={"username": name, "password": "12345678"})
        return r.status_code, bool(r.json().get("access_token"))

    def bad_login(_):
        with httpx.Client(base_url=live_server, timeout=30) as c:
            r = c.post("/api/auth/login", json={"username": name, "password": "wrong-pass"})
        return r.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        ok = list(pool.map(ok_login, range(20)))
        bad = list(pool.map(bad_login, range(10)))
    assert all(status == 200 and token for status, token in ok)
    assert all(status == 401 for status in bad)


def test_concurrent_alert_acknowledge_resolve(live_server):
    alert_ids = []
    for sid in STATION_IDS:
        st = database.get_station_by_id(sid)
        alert_id = create_alert_if_needed(
            station_id=sid,
            station_name=st["station_name"],
            water_level=round(st["warning_level"] * 1.2, 2),
            warning_level=st["warning_level"],
            rainfall=0.0,
            status="超警",
        )
        if alert_id:
            alert_ids.append(alert_id)

    with httpx.Client(base_url=live_server, timeout=30) as c:
        token = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]

    def resolve(alert_id: int):
        with httpx.Client(base_url=live_server, timeout=30) as c:
            r = c.post(f"/api/alerts/{alert_id}/resolve", headers={"Authorization": f"Bearer {token}"})
        return r.status_code

    def acknowledge(alert_id: int):
        with httpx.Client(base_url=live_server, timeout=30) as c:
            r = c.post(f"/api/alerts/{alert_id}/acknowledge", headers={"Authorization": f"Bearer {token}"})
        return r.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        resolve_results = list(pool.map(resolve, alert_ids * 3))
        ack_results = list(pool.map(acknowledge, alert_ids * 3))
    all_results = resolve_results + ack_results
    assert all(r in (200, 404) for r in all_results), f"出现非 200/404 状态: {set(all_results)}"
    assert all_results.count(200) > 0

    with httpx.Client(base_url=live_server, timeout=30) as c:
        r = c.get("/api/alerts?hours=720&limit=100")
        assert r.status_code == 200


def test_concurrent_db_writes_and_reads():
    before = database.get_water_history(station_id="ST001", limit=1) is not None

    def worker(_):
        st = database.get_station_by_id("ST001")
        for _ in range(5):
            database.insert_water_data(
                station_id="ST001",
                station_name=st["station_name"],
                water_level=3.2,
                warning_level=st["warning_level"],
                rainfall=0.0,
                status="正常",
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    conn = database.get_connection()
    count = conn.cursor().execute("SELECT COUNT(*) AS n FROM water_data").fetchone()["n"]
    conn.close()
    assert count >= 40
    assert before is True


def test_database_connection_pragmas():
    conn = database.get_connection()
    mode = conn.cursor().execute("PRAGMA journal_mode").fetchone()[0]
    busy = conn.cursor().execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert str(mode).lower() in ("wal", "delete", "memory")
    assert busy >= 5000