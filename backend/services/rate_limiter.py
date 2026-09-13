"""内存版滑动窗口限流器（第21阶段：生产安全与多用户稳定性）。

仅用于限制真实 AI 模型调用频率，防止高频请求引发成本与稳定性风险。
不引入 Redis 等外部依赖；进程重启后计数自动清零。

默认策略：单个身份（登录用户 或 客户端 IP）每分钟最多 10 次，
可通过环境变量 AI_RATE_LIMIT_PER_MINUTE 调整；规则分析不受限。
"""

import os
import threading
import time

_WINDOW_SECONDS = 60.0
_MAX_KEYS = 10000

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def _limit_per_minute() -> int:
    try:
        value = int(os.environ.get("AI_RATE_LIMIT_PER_MINUTE", "10"))
    except (TypeError, ValueError):
        value = 10
    return max(1, value)


def allow_ai_request(key: str) -> bool:
    """记录一次请求，若窗口内已超上限则拒绝（返回 False）。"""
    global _hits
    now = time.monotonic()
    limit = _limit_per_minute()
    with _lock:
        timestamps = _hits.setdefault(key, [])
        while timestamps and now - timestamps[0] >= _WINDOW_SECONDS:
            timestamps.pop(0)
        if len(timestamps) >= limit:
            return False
        timestamps.append(now)
        if len(_hits) > _MAX_KEYS:
            _hits = {k: v for k, v in _hits.items() if v}
        return True


def reset_ai_rate_limit() -> None:
    """清空计数（供测试与外部管理使用）。"""
    global _hits
    with _lock:
        _hits.clear()