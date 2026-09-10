import os
import sys

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