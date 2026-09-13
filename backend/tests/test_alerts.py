# 第17阶段：智能预警中心测试

import uuid

import database
import main
from fastapi.testclient import TestClient
from services.alert_service import evaluate_alert, create_alert_if_needed

client = TestClient(main.app)

WARNING_TYPE_BY_LEVEL = {
    "attention": "注意预警",
    "warning": "警戒预警",
    "danger": "超警预警",
}

LEVEL_CN = {
    "attention": "注意",
    "warning": "警戒",
    "danger": "超警",
}


def clear_alerts():
    conn = database.get_connection()
    conn.cursor().execute("DELETE FROM warning_records")
    conn.commit()
    conn.close()


def make_alert(station_id="ST001", station_name="都江堰水文站", water_level=4.25,
               warning_level=5.0, rainfall=0.0, alert_level="attention") -> int:
    return database.create_alert(
        station_id=station_id,
        station_name=station_name,
        water_level=water_level,
        warning_level_value=warning_level,
        rainfall=rainfall,
        alert_level=alert_level,
        warning_type=WARNING_TYPE_BY_LEVEL[alert_level],
        title=f"{station_name}({LEVEL_CN[alert_level]})水位预警",
        message=f"{station_name}触发{alert_level}等级预警",
    )


def admin_token() -> str:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def user_token() -> str:
    name = f"alrt_{uuid.uuid4().hex[:8]}"
    reg = client.post("/api/auth/register", json={"username": name, "password": "12345678"})
    assert reg.status_code == 200
    resp = client.post("/api/auth/login", json={"username": name, "password": "12345678"})
    assert resp.status_code == 200
    return resp.json()["access_token"]


# ──────────────────────────── 预警判定 evaluate_alert ────────────────────────────


def test_evaluate_alert_normal():
    assert evaluate_alert(3.0, 5.0, 0.0) is None


def test_evaluate_alert_tiny_rain_no_alert():
    assert evaluate_alert(3.0, 5.0, 3.0) is None


def test_evaluate_alert_attention():
    result = evaluate_alert(4.25, 5.0, 0.0)
    assert result is not None
    assert result["status"] == "注意"
    assert result["level"] == "attention"
    assert result["warning_type"] == "注意预警"


def test_evaluate_alert_warning():
    result = evaluate_alert(5.0, 5.0, 0.0)
    assert result["level"] == "warning"
    assert result["warning_type"] == "警戒预警"


def test_evaluate_alert_danger():
    result = evaluate_alert(5.5, 5.0, 0.0)
    assert result["level"] == "danger"
    assert result["warning_type"] == "超警预警"


def test_evaluate_alert_invalid_warning_level():
    assert evaluate_alert(4.0, 0.0, 0.0) is None
    assert evaluate_alert(4.0, -1.0, 0.0) is None


# ──────────────────────────── 自动生成 create_alert_if_needed ────────────────────────────


def test_create_alert_if_needed_normal_returns_none():
    clear_alerts()
    assert create_alert_if_needed("ST001", "都江堰水文站", 3.0, 5.0, 0.0) is None
    assert len(database.get_alerts()) == 0


def test_create_alert_if_needed_creates_pending_alert():
    clear_alerts()
    alert_id = create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    assert alert_id is not None
    alerts = database.get_alerts()
    assert len(alerts) == 1
    record = alerts[0]
    assert record["id"] == alert_id
    assert record["station_id"] == "ST001"
    assert record["warning_level"] == "attention"
    assert record["warning_level_value"] == 5.0
    assert record["status"] == "pending"
    assert record["title"]
    assert record["message"]


def test_create_alert_if_needed_duplicate_suppressed():
    clear_alerts()
    first = create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    assert first is not None
    second = create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    assert second is None
    assert len(database.get_alerts()) == 1


def test_create_alert_if_needed_same_station_different_level():
    clear_alerts()
    att = create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    assert att is not None
    warn = create_alert_if_needed("ST001", "都江堰水文站", 5.0, 5.0, 0.0)
    assert warn is not None
    assert len(database.get_alerts()) == 2


def test_create_alert_if_needed_different_station_same_level():
    clear_alerts()
    create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    second = create_alert_if_needed("ST002", "金堂水文站", 4.7, 5.5, 0.0)
    assert second is not None
    assert len(database.get_alerts()) == 2


def test_create_alert_if_needed_after_resolve_recreated():
    clear_alerts()
    first = create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    assert first is not None
    assert database.resolve_alert(first, "admin") is True
    second = create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    assert second is not None
    assert len(database.get_alerts()) == 2


def test_create_alert_if_needed_after_acknowledge_suppressed():
    clear_alerts()
    first = create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    assert first is not None
    database.acknowledge_alert(first, "admin")
    second = create_alert_if_needed("ST001", "都江堰水文站", 4.25, 5.0, 0.0)
    assert second is None
    assert len(database.get_alerts()) == 1


# ──────────────────────────── /api/alerts 查询 ────────────────────────────


def test_alerts_api_list_and_total():
    clear_alerts()
    make_alert(station_id="ST001", alert_level="attention")
    make_alert(station_id="ST001", alert_level="danger")
    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    record = data["items"][0]
    for field in ("id", "station_id", "station_name", "warning_level", "warning_type",
                  "title", "message", "water_level", "warning_level_value",
                  "rainfall", "status", "created_at",
                  "acknowledged_at", "acknowledged_by",
                  "resolved_at", "resolved_by"):
        assert field in record


def test_alerts_api_level_filter():
    clear_alerts()
    make_alert(station_id="ST001", alert_level="attention")
    make_alert(station_id="ST002", alert_level="danger")
    resp = client.get("/api/alerts", params={"level": "danger"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["warning_level"] == "danger"


def test_alerts_api_status_filter():
    clear_alerts()
    first = make_alert(station_id="ST001", alert_level="attention")
    second = make_alert(station_id="ST002", alert_level="warning")
    database.resolve_alert(first, "admin")
    database.acknowledge_alert(second, "admin")
    resp = client.get("/api/alerts", params={"status": "resolved"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    resp = client.get("/api/alerts", params={"status": "acknowledged"})
    assert resp.json()["total"] == 1
    resp = client.get("/api/alerts", params={"status": "pending"})
    assert resp.json()["total"] == 0


def test_alerts_api_station_filter():
    clear_alerts()
    make_alert(station_id="ST001", alert_level="attention")
    make_alert(station_id="ST002", alert_level="danger")
    resp = client.get("/api/alerts", params={"station_id": "ST002"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["station_id"] == "ST002"


def test_alerts_api_limit_offset():
    clear_alerts()
    make_alert(station_id="ST003", alert_level="warning")
    make_alert(station_id="ST004", alert_level="attention")
    resp = client.get("/api/alerts", params={"limit": 1, "offset": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 1


def test_alerts_api_hours_invalid_422():
    assert client.get("/api/alerts", params={"hours": 0}).status_code == 422
    assert client.get("/api/alerts", params={"hours": 721}).status_code == 422


def test_alerts_api_invalid_level_422():
    assert client.get("/api/alerts", params={"level": "critical"}).status_code == 422


def test_alerts_api_invalid_status_422():
    assert client.get("/api/alerts", params={"status": "done"}).status_code == 422


def test_alerts_api_invalid_station_422():
    assert client.get("/api/alerts", params={"station_id": "ST999"}).status_code == 422


def test_alerts_api_works_without_dist_dir():
    save = getattr(main, "_dist_dir", None)
    main._dist_dir = "/nonexistent/dist"
    try:
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        assert "items" in resp.json()
    finally:
        main._dist_dir = save


# ──────────────────────────── /api/alerts/summary 汇总 ────────────────────────────


def test_alerts_summary_counts():
    clear_alerts()
    d1 = make_alert(station_id="ST001", alert_level="danger")
    database.resolve_alert(d1, "admin")
    make_alert(station_id="ST002", alert_level="warning")
    w2 = make_alert(station_id="ST003", alert_level="warning")
    database.acknowledge_alert(w2, "admin")
    make_alert(station_id="ST004", alert_level="attention")
    make_alert(station_id="ST005", alert_level="attention")

    resp = client.get("/api/alerts/summary")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["total"] == 5
    assert summary["pending"] == 3
    assert summary["acknowledged"] == 1
    assert summary["resolved"] == 1
    assert summary["attention"] == 2
    assert summary["warning"] == 2
    assert summary["danger"] == 1
    assert summary["normal"] == 0


# ──────────────────────────── 确认 / 解除（管理员）────────────────────────────


def test_acknowledge_alert_success():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="warning")
    resp = client.post(
        f"/api/alerts/{alert_id}/acknowledge",
        headers={"Authorization": f"Bearer {admin_token()}"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    record = database.get_alerts(alert_level="warning")[0]
    assert record["status"] == "acknowledged"
    assert record["acknowledged_by"] == "admin"
    assert record["acknowledged_at"]


def test_resolve_alert_success():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="danger")
    resp = client.post(
        f"/api/alerts/{alert_id}/resolve",
        headers={"Authorization": f"Bearer {admin_token()}"},
    )
    assert resp.status_code == 200
    record = database.get_alerts(alert_level="danger")[0]
    assert record["status"] == "resolved"
    assert record["resolved_by"] == "admin"
    assert record["resolved_at"]

    conn = database.get_connection()
    row = conn.cursor().execute(
        "SELECT is_handled FROM warning_records WHERE id = ?", (alert_id,)
    ).fetchone()
    conn.close()
    assert row["is_handled"] == 1


def test_acknowledge_requires_admin():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="attention")
    token = user_token()
    resp = client.post(
        f"/api/alerts/{alert_id}/acknowledge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_resolve_requires_admin():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="warning")
    token = user_token()
    resp = client.post(
        f"/api/alerts/{alert_id}/resolve",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_acknowledge_requires_token():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="attention")
    resp = client.post(f"/api/alerts/{alert_id}/acknowledge")
    assert resp.status_code == 401


def test_resolve_requires_token():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="attention")
    resp = client.post(f"/api/alerts/{alert_id}/resolve")
    assert resp.status_code == 401


def test_acknowledge_missing_alert_404():
    resp = client.post(
        "/api/alerts/999999/acknowledge",
        headers={"Authorization": f"Bearer {admin_token()}"},
    )
    assert resp.status_code == 404


def test_resolve_missing_alert_404():
    resp = client.post(
        "/api/alerts/999999/resolve",
        headers={"Authorization": f"Bearer {admin_token()}"},
    )
    assert resp.status_code == 404


def test_acknowledge_then_resolve_flow():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="danger")
    token = admin_token()
    ack = client.post(f"/api/alerts/{alert_id}/acknowledge", headers={"Authorization": f"Bearer {token}"})
    assert ack.status_code == 200
    res = client.post(f"/api/alerts/{alert_id}/resolve", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    record = database.get_alerts(alert_level="danger")[0]
    assert record["status"] == "resolved"
    assert record["acknowledged_by"] == "admin"
    assert record["resolved_by"] == "admin"


# ──────────────────────────── 与既有预警 API 兼容 ────────────────────────────


def test_legacy_warnings_still_show_alert_records():
    clear_alerts()
    alert_id = create_alert_if_needed("ST001", "都江堰水文站", 5.0, 5.0, 0.0)
    assert alert_id is not None

    resp = client.get("/api/warnings?limit=10")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert any(w["id"] == alert_id for w in data)
    record = next(w for w in data if w["id"] == alert_id)
    assert record["warning_type"] == "警戒预警"
    assert record["is_handled"] == 0
    assert record["warning_level"] == 5.0

    resp = client.get("/api/warnings/active")
    assert resp.status_code == 200
    assert any(w["id"] == alert_id for w in resp.json()["data"])

    resp = client.post(f"/api/warnings/{alert_id}/handle", headers={"Authorization": f"Bearer {admin_token()}"})
    assert resp.status_code == 200
    resp = client.get("/api/warnings/active")
    assert not any(w["id"] == alert_id for w in resp.json()["data"])


def test_warnings_handle_requires_auth():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="warning")
    resp = client.post(f"/api/warnings/{alert_id}/handle")
    assert resp.status_code == 401


def test_warnings_handle_requires_admin():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="warning")
    resp = client.post(f"/api/warnings/{alert_id}/handle", headers={"Authorization": f"Bearer {user_token()}"})
    assert resp.status_code == 403
    conn = database.get_connection()
    row = conn.cursor().execute(
        "SELECT is_handled FROM warning_records WHERE id = ?", (alert_id,)
    ).fetchone()
    conn.close()
    assert row["is_handled"] == 0


def test_warnings_handle_admin_success():
    clear_alerts()
    alert_id = make_alert(station_id="ST001", alert_level="warning")
    resp = client.post(f"/api/warnings/{alert_id}/handle", headers={"Authorization": f"Bearer {admin_token()}"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    conn = database.get_connection()
    row = conn.cursor().execute(
        "SELECT is_handled FROM warning_records WHERE id = ?", (alert_id,)
    ).fetchone()
    conn.close()
    assert row["is_handled"] == 1
    active = client.get("/api/warnings/active").json()["data"]
    assert not any(w["id"] == alert_id for w in active)