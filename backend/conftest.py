import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

TEST_DB = os.path.join(os.environ.get("TEMP", "/tmp"), "water_monitor_test.db")

try:
    os.remove(TEST_DB)
except OSError:
    pass

os.environ["WATER_MONITOR_DB_PATH"] = TEST_DB
os.environ["ADMIN_PASSWORD"] = "admin123"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key")
os.environ.setdefault("WATER_DATA_PROVIDER", "mock")


@pytest.fixture(autouse=True)
def _reset_ai_state():
    """每个测试前后清空 AI 结果缓存与限流计数，避免跨测试干扰。"""
    from services.ai_analysis_service import reset_ai_analysis_cache
    from services.rate_limiter import reset_ai_rate_limit

    reset_ai_analysis_cache()
    reset_ai_rate_limit()
    yield
    reset_ai_analysis_cache()
    reset_ai_rate_limit()