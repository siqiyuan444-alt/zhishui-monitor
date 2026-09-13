"""Stage 21：生产安全加固测试。

覆盖：
- 登录不泄露账号是否存在（禁用账号 / 错误密码 / 不存在用户统一 401）；
- 权限隔离：普通用户无法访问管理员接口（用户管理 / 预警处理）；
- 输入安全：超长参数统一 4xx，不 500、不回显内部细节；
- 响应头：X-Content-Type-Options / X-Frame-Options / Referrer-Policy / CSP；
- 真实 AI 模式限流（429），且不影响规则分析与其它水情接口；
- 静态资源路径穿越被拒绝，且不泄露敏感文件内容。
"""

import json
import os
import uuid

import database
import main
import services.ai_analysis_service as ai_service
from fastapi.testclient import TestClient

client = TestClient(main.app)


def unique_username(prefix: str = "secur") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def register(username: str, password: str = "12345678"):
    resp = client.post("/api/auth/register", json={"username": username, "password": password})
    assert resp.status_code == 200
    return username


def user_token(username: str, password: str = "12345678") -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def admin_token() -> str:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _disable_user(username: str):
    conn = database.get_connection()
    conn.cursor().execute("UPDATE users SET is_active = 0 WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def _fake_openai_resp(content: str):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    return FakeResp()


def _valid_ai_content() -> str:
    base = {
        "risk_level": "attention",
        "risk_score": 55,
        "summary": "各站水位整体平稳，个别站点需适当关注。",
        "key_findings": ["ST001 水位略高于注意阈值"],
        "trend_analysis": {"overall": "过去 24 小时多数站点水位平稳。", "stations": []},
        "abnormal_stations": [],
        "recommendations": ["按常规频率继续监测水位变化。"],
        "generated_at": "2026-01-01T00:00:00",
        "analysis_source": "ai_model",
        "note": "",
    }
    return json.dumps(base, ensure_ascii=False)


# ──────────────────────────── 登录：不泄露用户是否存在 ────────────────────────────

def test_login_disabled_user_returns_same_401_message():
    name = register(unique_username("off"))
    _disable_user(name)
    resp = client.post("/api/auth/login", json={"username": name, "password": "12345678"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "用户名或密码错误"
    assert "禁用" not in resp.text


def test_login_wrong_password_same_message_as_missing_user():
    name = register(unique_username("enum"))
    wrong = client.post("/api/auth/login", json={"username": name, "password": "wrong-pass"})
    missing = client.post("/api/auth/login", json={"username": unique_username("ghost"), "password": "wrong-pass"})
    assert wrong.status_code == 401
    assert missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"] == "用户名或密码错误"


# ──────────────────────────── 权限隔离 ────────────────────────────

def test_regular_user_cannot_access_admin_apis():
    token = user_token(register(unique_username("lowpriv")))
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/users", headers=headers).status_code == 403
    assert client.post("/api/users", json={"username": "x", "password": "12345678", "role": "admin"}, headers=headers).status_code == 403
    assert client.delete("/api/users/1", headers=headers).status_code == 403
    assert client.post("/api/users/2/role", json={"role": "admin"}, headers=headers).status_code == 403
    assert client.post("/api/warnings/1/handle", headers=headers).status_code == 403
    assert client.post("/api/alerts/1/acknowledge", headers=headers).status_code == 403
    assert client.post("/api/alerts/1/resolve", headers=headers).status_code == 403


def test_admin_self_protection_rules_still_work():
    headers = {"Authorization": f"Bearer {admin_token()}"}
    me = client.get("/api/auth/me", headers=headers).json()
    assert client.delete(f"/api/users/{me['id']}", headers=headers).status_code == 400
    assert client.post(f"/api/users/{me['id']}/role", json={"role": "user"}, headers=headers).status_code == 400
    assert client.post("/api/users/1/role", json={"role": "superuser"}, headers=headers).status_code == 400


def test_registered_user_me_endpoint_does_not_leak_admin_fields():
    token = user_token(register(unique_username("meinfo")))
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "user"
    assert "password_hash" not in resp.text


# ──────────────────────────── 输入安全 ────────────────────────────

def test_oversized_station_id_rejected():
    long_id = "S" * 80
    resp = client.get(f"/api/water-data?station_id={long_id}")
    assert resp.status_code == 422
    assert "Traceback" not in resp.text


def test_oversized_auth_inputs_rejected():
    resp = client.post("/api/auth/login", json={"username": "u" * 100, "password": "p" * 300})
    assert resp.status_code == 422
    long_pwd = client.post("/api/auth/register", json={"username": unique_username("w"), "password": "p" * 201})
    assert long_pwd.status_code == 422
    long_name = client.post("/api/auth/register", json={"username": "u" * 31, "password": "12345678"})
    assert long_name.status_code == 422
    assert "Traceback" not in resp.text


def test_validation_errors_do_not_leak_internals():
    resp = client.post("/api/auth/login", json={"username": "u" * 100, "password": "p" * 300})
    resp2 = client.get(f"/api/water-data?station_id={'S' * 80}")
    for r in (resp, resp2):
        body = r.text.lower()
        assert "argon2" not in body
        assert "sqlite" not in body
        assert "password_hash" not in body
        assert "jwt" not in body
        assert "water_monitor.db" not in body
        assert "traceback" not in body


# ──────────────────────────── 响应安全头 ────────────────────────────

def test_security_headers_present():
    resp = client.get("/api/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "SAMEORIGIN"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "content-security-policy" in resp.headers


# ──────────────────────────── AI 限流 ────────────────────────────

def test_ai_rate_limit_returns_429_when_over_limit(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_MODEL", "test-model-name")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.setattr(
        ai_service.httpx,
        "post",
        lambda *a, **kw: _fake_openai_resp(_valid_ai_content()),
    )
    statuses = [client.get("/api/ai-analysis").status_code for _ in range(4)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    body = client.get("/api/ai-analysis").json()
    assert "频繁" in body["detail"]


def test_ai_rate_limit_rule_based_never_limited(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "rule_based")
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "1")
    for _ in range(6):
        resp = client.get("/api/ai-analysis")
        assert resp.status_code == 200
        assert resp.json()["analysis_source"] == "rule_based"


def test_ai_rate_limit_does_not_affect_other_endpoints(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    monkeypatch.setenv("AI_ANALYSIS_CACHE_SECONDS", "0")
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setattr(
        ai_service.httpx,
        "post",
        lambda *a, **kw: _fake_openai_resp(_valid_ai_content()),
    )
    assert client.get("/api/ai-analysis").status_code == 200
    assert client.get("/api/ai-analysis").status_code == 429
    assert client.get("/api/water-data?station_id=ST001").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/water-history?station_id=ST001").status_code == 200


def test_ai_rate_limit_resets_between_tests(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER", "rule_based")
    assert client.get("/api/ai-analysis").status_code == 200


# ──────────────────────────── 静态资源路径穿越 ────────────────────────────

def test_serve_spa_path_traversal_rejected():
    if not os.path.isdir(main._dist_dir):
        import pytest

        pytest.skip("frontend dist 目录不存在，serve_spa 未注册")
    resp = client.get("/..%2F..%2Fbackend%2F.env")
    assert resp.status_code == 404
    assert "JWT_SECRET_KEY" not in resp.text
    resp2 = client.get("/assets%2F..%2F..%2Fbackend%2F.env")
    assert resp2.status_code == 404
    assert "JWT_SECRET_KEY" not in resp2.text