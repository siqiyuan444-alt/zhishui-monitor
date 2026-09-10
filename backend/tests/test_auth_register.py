import uuid

import database
import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def unique_username(prefix: str = "regusr") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def register(username: str, password: str, confirm_password=None, extra=None) -> object:
    payload = {"username": username, "password": password}
    if confirm_password is not None:
        payload["confirm_password"] = confirm_password
    if extra:
        payload.update(extra)
    return client.post("/api/auth/register", json=payload)


def fetch_user(username: str):
    conn = database.get_connection()
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT id, username, password_hash, role, is_active, created_at FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ──────────────────────────── 注册功能测试 ────────────────────────────

def test_register_success():
    name = unique_username()
    resp = register(name, "12345678")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["message"] == "注册成功"


def test_register_duplicate_username():
    name = unique_username("dup")
    resp1 = register(name, "12345678")
    assert resp1.status_code == 200
    resp2 = register(name, "87654321")
    assert resp2.status_code == 400
    assert resp2.json()["detail"] == "用户名已存在"


def test_register_short_password():
    resp = register(unique_username(), "1234567")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "密码长度不能少于8位"


def test_register_short_username():
    resp = register("ab", "12345678")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "用户名长度需在3到30个字符之间"


def test_register_empty_username():
    resp = register("", "12345678")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "用户名不能为空"


def test_register_password_mismatch():
    resp = register(unique_username(), "12345678", confirm_password="87654321")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "两次密码输入不一致"


def test_register_role_is_user():
    name = unique_username()
    register(name, "12345678")
    user = fetch_user(name)
    assert user is not None
    assert user["role"] == "user"


def test_register_role_admin_ignored():
    name = unique_username("hacker")
    register(name, "12345678", extra={"role": "admin"})
    user = fetch_user(name)
    assert user is not None
    assert user["role"] == "user"


def test_registered_user_can_login():
    name = unique_username()
    register(name, "12345678")
    resp = client.post("/api/auth/login", json={"username": name, "password": "12345678"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["user"]["username"] == name
    assert data["user"]["role"] == "user"


def test_password_not_stored_in_plaintext():
    name = unique_username()
    password = "12345678"
    register(name, password)
    user = fetch_user(name)
    assert user is not None
    assert user["password_hash"] != password
    assert user["password_hash"].startswith("$argon2")
    from passlib.hash import argon2

    assert argon2.verify(password, user["password_hash"]) is True


def test_registered_user_has_no_admin_privilege():
    name = unique_username()
    register(name, "12345678")
    login = client.post("/api/auth/login", json={"username": name, "password": "12345678"}).json()
    token = login["access_token"]
    resp = client.get("/api/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# ──────────────────────────── 原有功能回归测试 ────────────────────────────

def test_admin_login_still_works():
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["role"] == "admin"


def test_auth_me_requires_token():
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_registered_database_admin_not_deleted():
    user = fetch_user("admin")
    assert user is not None
    assert user["role"] == "admin"


def test_stations_api_works():
    resp = client.get("/api/stations")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) > 0


def test_water_data_api_works():
    resp = client.get("/api/water-data?station_id=ST001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["station_id"] == "ST001"
    assert "water_level" in data


def test_water_data_all_api_works():
    resp = client.get("/api/water-data-all")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) > 0


def test_rainfall_summary_api_works():
    resp = client.get("/api/rainfall-summary?station_id=ST001")
    assert resp.status_code == 200
    assert resp.json()["station_id"] == "ST001"


def test_warnings_api_works():
    resp = client.get("/api/warnings?limit=10")
    assert resp.status_code == 200
    assert "data" in resp.json()