import os
import random
from datetime import datetime, timedelta

import jwt
from fastapi import FastAPI, Query, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from passlib.hash import argon2
from pydantic import BaseModel

from database import (
    get_stations,
    get_station_by_id,
    insert_water_data,
    get_water_history,
    get_all_latest_water_data,
    get_rainfall_summary,
    calculate_status,
    generate_rainfall,
    create_warning,
    get_warnings,
    get_active_warnings,
    get_warnings_summary,
    handle_warning,
    get_latest_warning,
    WATER_RANGES,
    get_user_by_username,
    get_user_by_id,
    get_all_users,
    create_user,
    delete_user,
    update_user_role,
)

JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "60"))

app = FastAPI()

_cors_origins = os.environ.get("CORS_ORIGINS", "").strip()
if _cors_origins:
    allow_origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]
else:
    allow_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

_dist_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "dist")


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    confirm_password: str = ""


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


class UpdateRoleRequest(BaseModel):
    role: str


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_current_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            raise HTTPException(status_code=401, detail="无效的token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的token")
    user = get_user_by_id(user_id)
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    return user


def require_admin(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ──────────────────────────── Auth API ────────────────────────────

@app.post("/api/auth/login")
def login(req: LoginRequest):
    user = get_user_by_username(req.username)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="账户已被禁用")
    if not argon2.verify(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token({"user_id": user["id"], "username": user["username"], "role": user["role"]})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
        },
    }


@app.post("/api/auth/register")
def register(req: RegisterRequest):
    username = (req.username or "").strip()
    password = req.password or ""

    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if len(username) < 3 or len(username) > 30:
        raise HTTPException(status_code=400, detail="用户名长度需在3到30个字符之间")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码长度不能少于8位")
    if req.confirm_password and req.confirm_password != password:
        raise HTTPException(status_code=400, detail="两次密码输入不一致")

    if get_user_by_username(username):
        raise HTTPException(status_code=400, detail="用户名已存在")

    password_hash = argon2.hash(password)
    # 自主注册用户强制为普通用户 user，客户端传入的任何 role 都会被忽略
    user_id = create_user(username=username, password_hash=password_hash, role="user")
    return {"success": True, "message": "注册成功", "user_id": user_id}


@app.get("/api/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "is_active": user["is_active"],
        "created_at": user["created_at"],
    }


@app.post("/api/auth/logout")
def logout():
    return {"success": True}


# ──────────────────────────── User Management API ────────────────────────────

@app.get("/api/users")
def list_users(admin: dict = Depends(require_admin)):
    users = get_all_users()
    return {"data": users}


@app.post("/api/users")
def create_user_endpoint(req: CreateUserRequest, admin: dict = Depends(require_admin)):
    existing = get_user_by_username(req.username)
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")
    if req.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="角色必须是 admin 或 user")
    password_hash = argon2.hash(req.password)
    user_id = create_user(username=req.username, password_hash=password_hash, role=req.role)
    return {"success": True, "user_id": user_id}


@app.delete("/api/users/{user_id}")
def delete_user_endpoint(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="不能删除自己")
    success = delete_user(user_id)
    if not success:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"success": True}


@app.post("/api/users/{user_id}/role")
def update_user_role_endpoint(user_id: int, req: UpdateRoleRequest, admin: dict = Depends(require_admin)):
    if req.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="角色必须是 admin 或 user")
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    success = update_user_role(user_id, req.role)
    if not success:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"success": True}


# ──────────────────────────── Original Monitoring API (unchanged) ────────────────────────────

@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "message": "智水监测后端运行正常",
    }


@app.get("/api/stations")
def stations():
    data = get_stations()
    return {"data": data}


@app.get("/api/water-data")
def get_water_data(station_id: str = Query(default="ST001")):
    station = get_station_by_id(station_id)
    if not station:
        return {"error": f"水文站 {station_id} 不存在"}

    warning_level = station["warning_level"]
    lo, hi = WATER_RANGES.get(station_id, (3.0, 6.0))
    water_level = round(random.uniform(lo, hi), 2)
    rainfall = generate_rainfall()
    status = calculate_status(water_level, warning_level, rainfall)

    insert_water_data(
        station_id=station_id,
        station_name=station["station_name"],
        water_level=water_level,
        warning_level=warning_level,
        rainfall=rainfall,
        status=status,
    )

    if status != "正常":
        create_warning(
            station_id=station_id,
            station_name=station["station_name"],
            water_level=water_level,
            warning_level=warning_level,
            rainfall=rainfall,
            status=status,
        )

    return {
        "station_id": station_id,
        "station_name": station["station_name"],
        "water_level": water_level,
        "warning_level": warning_level,
        "rainfall": rainfall,
        "status": status,
    }


@app.get("/api/water-data-all")
def get_water_data_all():
    data = get_all_latest_water_data()
    return {"data": data}


@app.get("/api/water-history")
def water_history(
    station_id: str = Query(default="ST001"),
    limit: int = Query(default=20, ge=1, le=500),
):
    data = get_water_history(station_id=station_id, limit=limit)
    return {"data": data}


@app.get("/api/rainfall-summary")
def rainfall_summary(station_id: str = Query(default="ST001")):
    data = get_rainfall_summary(station_id=station_id)
    return data


@app.get("/api/warnings")
def warnings_list(limit: int = Query(default=50, ge=1, le=500)):
    data = get_warnings(limit=limit)
    return {"data": data}


@app.get("/api/warnings/active")
def warnings_active():
    data = get_active_warnings()
    return {"data": data}


@app.get("/api/warnings-summary")
def warnings_summary():
    return get_warnings_summary()


@app.post("/api/warnings/{warning_id}/handle")
def handle_warning_endpoint(warning_id: int):
    success = handle_warning(warning_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"预警记录 {warning_id} 不存在")
    return {"success": True}


if os.path.isdir(_dist_dir):
    _assets_dir = os.path.join(_dist_dir, "assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        if full_path.startswith("assets/"):
            file_path = os.path.join(_dist_dir, full_path)
            if os.path.isfile(file_path):
                return FileResponse(file_path)
            raise HTTPException(status_code=404, detail="Not found")
        if full_path and os.path.isfile(os.path.join(_dist_dir, full_path)):
            return FileResponse(os.path.join(_dist_dir, full_path))
        return FileResponse(os.path.join(_dist_dir, "index.html"))
