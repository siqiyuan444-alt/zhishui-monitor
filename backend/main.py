import os
import random

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

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
)

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
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=[],
)

_dist_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "dist")


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
