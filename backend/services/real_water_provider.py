"""成都政务开放数据平台「根据测站编码查询雨情信息」实时数据源适配器。

对接官方接口：
    GET https://www.chengdu.gov.cn/data/gateway/api/1/sswj/gjczbmcxyqxx?stcd=测站编码

认证方式：
    官方「基于签名认证的调用」，通过自定义请求头传递：
        X-Client-Id    申请到的 Client ID
        X-Timestamp    当前毫秒级时间戳（动态生成）
        X-Nonce        每次请求随机生成的字符串（动态生成）
        X-Signature    Base64( HmacSHA256(X-Client-Id + X-Timestamp + X-Nonce, Client Secret) )

重要：
    这是「雨情 API」，只提供降雨量（drp / dyp），不提供水位。
    本适配器绝不允许把 drp / dyp 映射成 water_level；
    水位与警戒水位仍来自 MockWaterProvider（现有模拟数据保持不变），
    官方真实降雨量会覆盖记录中的 rainfall 字段。

数据来源约定（写入 water_data.source / data_quality）：
    成都官方 API 成功           -> source=chengdu_open_data
    成功但数据不完整(dyp/missing) -> data_quality=degraded
    请求失败后使用 Mock          -> source=mock_fallback / data_quality=degraded
    返回数据格式无效            -> source=invalid_fallback / data_quality=degraded

环境变量：
    CHENGDU_API_BASE_URL    接口地址（默认官方地址）
    CHENGDU_CLIENT_ID       Client ID
    CHENGDU_CLIENT_SECRET   Client Secret（禁止写死/提交到 Git）
    CHENGDU_API_TIMEOUT     请求超时秒数（默认 10）
    CHENGDU_STATION_CODES   JSON 映射，例如 {"ST001": "xxxxxx", ...}（默认空）
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime

from .water_data_provider import STATIONS, ProviderError, ProviderNotConfiguredError, WaterDataProvider
from .mock_water_provider import build_mock_record, calculate_status
from .data_normalizer import (
    SOURCE_CHENGDU_OPEN_DATA,
    SOURCE_MOCK_FALLBACK,
    SOURCE_INVALID_FALLBACK,
    QUALITY_GOOD,
    QUALITY_DEGRADED,
    _parse_timestamp,
)

logger = logging.getLogger("chengdu_provider")

DEFAULT_API_BASE_URL = "https://www.chengdu.gov.cn/data/gateway/api/1/sswj/gjczbmcxyqxx"

_REQUEST_FIELDS = ("drp", "dyp", "intv", "pdr", "tm", "stcd", "wth", "flag", "id", "last_modify_time")


def current_timestamp_ms() -> str:
    """返回当前时间的毫秒级时间戳字符串（用于 X-Timestamp）。"""
    return str(int(time.time() * 1000))


def generate_nonce() -> str:
    """每次请求动态生成随机字符串（用于 X-Nonce）。"""
    return uuid.uuid4().hex


def build_signature(client_id: str, timestamp_ms: str, nonce: str, client_secret: str) -> str:
    """计算官方的 X-Signature：Base64( HmacSHA256(client_id + timestamp + nonce, client_secret) )。"""
    message = f"{client_id}{timestamp_ms}{nonce}"
    digest = hmac.new(
        client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _safe_float(value):
    """尝试把官方雨量字段转成 float；空值/非法值返回 None。"""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return result


def _extract_records(payload):
    """从官方响应中宽容地提取记录列表。

    官方返回结构未知，这里兼容 dict / list 包裹形式，
    逐级查找 data / result / records / content / list / items / rows。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "result", "records", "content", "list", "items", "rows", "values"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if _looks_like_record(payload):
            return [payload]
    return []


def _looks_like_record(item):
    """判断 dict 是否像一条雨情记录（包含官方字段中的关键字段）。"""
    if not isinstance(item, dict):
        return False
    return bool(item.get("stcd") is not None) or any(item.get(f) is not None for f in ("tm", "drp", "dyp"))


def _http_get_json(url: str, params: dict, headers: dict, timeout: float):
    """发送 GET 请求并解析 JSON。

    使用已安装的 httpx 进行请求；任何异常统一转换为 ChengduApiError。
    独立成模块级函数，便于测试直接替换 mock。
    """
    try:
        import httpx
    except ImportError:
        raise ChengduApiError("network", "httpx 未安装")  # noqa: B904

    try:
        response = httpx.get(url, params=params, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        raise ChengduApiError("timeout", "请求超时")  # noqa: B904
    except httpx.RequestError as exc:
        raise ChengduApiError("network", f"网络/DNS 错误: {exc}")  # noqa: B904

    if response.status_code >= 400:
        raise ChengduApiError(
            "http",
            f"HTTP {response.status_code}",
            status_code=response.status_code,
        )

    try:
        return response.json()
    except ValueError:
        raise ChengduApiError("json", "响应不是合法 JSON")  # noqa: B904


class ChengduApiError(ProviderError):
    """成都官方 API 调用异常，kind 标识错误类别。"""

    def __init__(self, kind: str, message: str = "", status_code: int = None):
        self.kind = kind
        self.status_code = status_code
        super().__init__(f"[{kind}] {message}")


class RealWaterProvider(WaterDataProvider):
    """成都政务开放数据（雨情）提供器。"""

    name = SOURCE_CHENGDU_OPEN_DATA
    display_name = "成都政务开放数据(雨情)"

    def __init__(self):
        self.base_url = os.environ.get("CHENGDU_API_BASE_URL", "").strip() or DEFAULT_API_BASE_URL
        self.client_id = os.environ.get("CHENGDU_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("CHENGDU_CLIENT_SECRET", "").strip()
        try:
            self.timeout = float(os.environ.get("CHENGDU_API_TIMEOUT", "10").strip() or 10)
        except ValueError:
            self.timeout = 10
        self.station_codes = self._load_station_codes()

    # ── 配置 ──

    def _load_station_codes(self) -> dict:
        """从 CHENGDU_STATION_CODES JSON 环境变量加载 系统站点ID -> 官方stcd 映射。"""
        mapping = {}
        raw = os.environ.get("CHENGDU_STATION_CODES", "").strip()
        if not raw:
            return mapping
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("CHENGDU_STATION_CODES 不是合法 JSON，忽略该配置")
            return mapping
        if isinstance(data, dict):
            for key, value in data.items():
                mapping[str(key).strip()] = str(value).strip()
        return mapping

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def get_station_code(self, station_id: str) -> str:
        """返回系统站点 ID 对应的官方测站编码；未配置返回空字符串。"""
        return self.station_codes.get(str(station_id).strip(), "")

    def _check_configured(self):
        if not self.client_id:
            raise ProviderNotConfiguredError("成都官方数据源未配置 Client ID（CHENGDU_CLIENT_ID）")
        if not self.client_secret:
            raise ProviderNotConfiguredError("成都官方数据源未配置 Client Secret（CHENGDU_CLIENT_SECRET）")

    # ── 连通性诊断 ──

    def check_connection(self) -> dict:
        """成都官方数据源连通性诊断（内部测试能力，供后端调用/诊断 API 使用）。

        验证：
            1. 环境变量是否配置（Client ID / Client Secret / API Base URL）
            2. 签名是否能够生成
            3. HTTP 请求是否能够发送 / API HTTP status
            4. 认证是否通过（401/403 视为未通过）
            5. 返回数据是否有效（存在 drp/dyp 雨量字段）

        安全约定：
            - 绝不返回 / 打印 Client Secret、完整 Authorization、Signature 或敏感环境变量值；
            - 只返回 configured / reachable / authenticated / data_valid 等布尔标记与说明文本。
            若未配置真实的官方测站编码，不会发起任何对外请求（禁止编造/猜测 stcd）。
        """
        result = {
            "provider": self.name,
            "display_name": self.display_name,
            "configured": False,
            "signature_ok": False,
            "reachable": False,
            "authenticated": False,
            "data_valid": False,
            "data_quality": QUALITY_DEGRADED,
            "tested_stcd": False,
            "reason": "",
        }

        if not self.base_url:
            result["reason"] = "未配置 CHENGDU_API_BASE_URL"
            return result
        if not self.client_id:
            result["reason"] = "未配置 CHENGDU_CLIENT_ID"
            return result
        if not self.client_secret:
            result["reason"] = "未配置 CHENGDU_CLIENT_SECRET"
            return result
        result["configured"] = True

        try:
            build_signature(self.client_id, current_timestamp_ms(), generate_nonce(), self.client_secret)
            result["signature_ok"] = True
        except Exception:  # noqa: BLE001 - 签名失败不崩溃
            result["reason"] = "签名生成失败"
            return result

        real_stcd = next((code for code in self.station_codes.values() if code), "")
        if not real_stcd:
            result["reason"] = "未配置官方测站编码(stcd)，禁止编造/猜测编码，未发起真实请求"
            return result
        result["tested_stcd"] = True

        try:
            payload = _http_get_json(
                self.base_url,
                params={"stcd": real_stcd},
                headers=self._build_headers(),
                timeout=self.timeout,
            )
        except ChengduApiError as exc:
            if exc.kind == "http":
                result["reachable"] = True  # 服务器可达（返回了 HTTP 状态码）
                if exc.status_code in (401, 403):
                    result["reason"] = f"服务器可达，但认证/授权失败（HTTP {exc.status_code}）"
                else:
                    result["reason"] = f"服务器可达，但返回 HTTP {exc.status_code}"
            elif exc.kind in ("timeout", "network"):
                result["reason"] = f"无法连接官方接口: {exc}"
            else:
                result["reachable"] = True
                result["reason"] = f"接口可达，但响应内容异常: {exc}"
            return result

        result["reachable"] = True
        result["authenticated"] = True  # 未收到 401/403，视为认证通过
        records = _extract_records(payload)
        row = next((r for r in records if _looks_like_record(r)), None)
        drp = _safe_float(row.get("drp")) if row else None
        dyp = _safe_float(row.get("dyp")) if row else None
        if drp is None and dyp is None:
            result["reason"] = "服务器可达，但响应中不存在有效雨情记录（缺少 drp/dyp）"
            return result
        result["data_valid"] = True
        result["data_quality"] = QUALITY_GOOD if drp is not None else QUALITY_DEGRADED
        result["reason"] = "连接成功"
        return result

    # ── 接口请求 ──

    def _build_headers(self) -> dict:
        timestamp = current_timestamp_ms()
        nonce = generate_nonce()
        signature = build_signature(self.client_id, timestamp, nonce, self.client_secret)
        return {
            "X-Client-Id": self.client_id,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

    def _fetch_station(self, stcd: str) -> dict:
        """按测站编码请求官方接口，返回解析后的雨情信息。"""
        payload = _http_get_json(
            self.base_url,
            params={"stcd": stcd},
            headers=self._build_headers(),
            timeout=self.timeout,
        )
        records = _extract_records(payload)
        if not records:
            raise ChengduApiError("empty", "接口返回空数据或结构无效")

        row = next((r for r in records if _looks_like_record(r) and str(r.get("stcd")) == str(stcd)), None)
        if row is None:
            row = next((r for r in records if _looks_like_record(r)), None)
        if row is None:
            raise ChengduApiError("fields", "响应中不存在有效雨情记录")

        drp = _safe_float(row.get("drp"))
        dyp = _safe_float(row.get("dyp"))
        if drp is None and dyp is None:
            raise ChengduApiError("fields", "缺少 drp / dyp 雨量字段")

        return {
            "stcd": str(row.get("stcd") or stcd),
            "tm": row.get("tm"),
            "wth": row.get("wth"),
            "intv": row.get("intv"),
            "pdr": row.get("pdr"),
            "drp": drp,
            "dyp": dyp,
            "rainfall": drp if drp is not None else dyp,
            "origin": "drp" if drp is not None else "dyp",
            "quality": QUALITY_GOOD if drp is not None else QUALITY_DEGRADED,
        }

    # ── 数据组装 ──

    def _resolve_timestamp(self, tm) -> str:
        parsed = _parse_timestamp(tm)
        if parsed is not None:
            return parsed.isoformat(timespec="seconds")
        return datetime.now().isoformat(timespec="seconds")

    def _build_real_record(self, station: dict, rain: dict) -> dict:
        """把官方真实降雨量与 Mock 水位合并为一条统一记录。

        水位/警戒水位来源不变（Mock），rainfall 使用官方真实值，
        status 由水位、警戒水位、真实降雨量重新计算，绝不把 drp/dyp 当作水位。
        """
        now_iso = self._resolve_timestamp(rain.get("tm"))
        base = build_mock_record(station, timestamp=now_iso)
        water_level = float(base["water_level"])
        warning_level = float(station["warning_level"])
        rainfall = float(rain["rainfall"])

        record = {
            "station_id": station["station_id"],
            "station_name": station["station_name"],
            "timestamp": now_iso,
            "water_level": water_level,
            "warning_level": warning_level,
            "rainfall": rainfall,
            "status": calculate_status(water_level, warning_level, rainfall),
            "latitude": station["latitude"],
            "longitude": station["longitude"],
            "source": SOURCE_CHENGDU_OPEN_DATA,
            "data_quality": rain["quality"],
            "rainfall_origin": rain["origin"],
            "official:stcd": rain["stcd"],
            "official:tm": rain["tm"],
            "official:wth": rain["wth"],
            "official:drp": rain["drp"],
            "official:dyp": rain["dyp"],
        }
        return record

    def _mock_record(self, station: dict, source: str, quality: str, reason: str = "") -> dict:
        """生成一条带 fallback 标签的 Mock 记录。"""
        record = build_mock_record(station)
        record["source"] = source
        record["data_quality"] = quality
        if reason:
            record["quality_reason"] = reason
        return record

    # ── WaterDataProvider 接口 ──

    def get_stations(self) -> list:
        return [dict(s) for s in STATIONS]

    def get_station_data(self, station_id: str) -> dict:
        """获取某个站点的一条数据：真实降雨量(Mock水位) 或 invalid_fallback 记录。"""
        self._check_configured()
        station = next((s for s in STATIONS if s["station_id"] == station_id), None)
        if not station:
            raise ProviderError(f"未知站点: {station_id}")

        stcd = self.get_station_code(station_id)
        if not stcd:
            raise ProviderNotConfiguredError("未配置官方测站编码")

        try:
            rain = self._fetch_station(stcd)
        except ChengduApiError as exc:
            if exc.kind in ("empty", "fields"):
                logger.warning("成都雨情数据格式无效 station=%s reason=%s", station_id, exc)
                return self._mock_record(station, SOURCE_INVALID_FALLBACK, QUALITY_DEGRADED, str(exc))
            raise
        return self._build_real_record(station, rain)

    def get_current_data(self) -> list:
        """返回全部站点当前数据，逐站尝试真实雨情；请求级失败时抛错交给采集器兜底。"""
        self._check_configured()
        if not any(c for c in self.station_codes.values() if c):
            raise ProviderNotConfiguredError("未配置官方测站编码")
        records = []
        for station in STATIONS:
            stcd = self.get_station_code(station["station_id"])
            if not stcd:
                records.append(
                    self._mock_record(station, SOURCE_MOCK_FALLBACK, QUALITY_DEGRADED, "未配置官方测站编码")
                )
                continue
            try:
                rain = self._fetch_station(stcd)
            except ChengduApiError as exc:
                if exc.kind in ("empty", "fields"):
                    records.append(
                        self._mock_record(station, SOURCE_INVALID_FALLBACK, QUALITY_DEGRADED, str(exc))
                    )
                    continue
                raise
            records.append(self._build_real_record(station, rain))
        return records

    def get_history_data(self, station_id: str, limit: int = 20) -> list:
        # 官方接口仅提供实时雨情，历史数据沿用现有 Mock 生成逻辑。
        from .mock_water_provider import MockWaterProvider

        return MockWaterProvider().get_history_data(station_id, limit)