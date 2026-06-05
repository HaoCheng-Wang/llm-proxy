import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root or backend directory
_env_paths = [
    Path(__file__).resolve().parent.parent / ".env",   # project root
    Path(__file__).resolve().parent / ".env",           # backend/
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p, override=True)
        break

# ---------------------------------------------------------------------------
# 数据库 — 从独立字段拼出连接串
# ---------------------------------------------------------------------------
_DB_USER = os.getenv("DATABASE_USER", "root")
_DB_PASS = os.getenv("DATABASE_PASSWORD", "root")
_DB_HOST = os.getenv("DATABASE_HOST", "localhost")
_DB_PORT = os.getenv("DATABASE_PORT", "3306")
_DB_NAME = os.getenv("DATABASE_NAME", "llm_proxy")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"mysql+pymysql://{_DB_USER}:{_DB_PASS}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}",
)

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY must be set in .env file.\n"
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    )

# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
API_PORT = int(os.getenv("API_PORT", "3998"))
API_WORKERS = int(os.getenv("API_WORKERS", "2"))
DISPLAY_IP = os.getenv("DISPLAY_IP", "your-server-ip")
PROXY_PORT_START = int(os.getenv("PROXY_PORT_START", "4000"))
PROXY_PORT_END = int(os.getenv("PROXY_PORT_END", "5000"))
MAX_PORTS_PER_USER = int(os.getenv("MAX_PORTS_PER_USER", "10"))

# CORS 允许的源，逗号分隔。留空则仅允许 localhost 开发地址。
# 示例: CORS_ORIGINS=https://example.com,https://admin.example.com
_cors_raw = os.getenv("CORS_ORIGINS", "")
if _cors_raw:
    CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]
else:
    CORS_ORIGINS = ["http://localhost:3999", "http://127.0.0.1:3999"]

# ---------------------------------------------------------------------------
# 管理员
# ---------------------------------------------------------------------------
DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")

# ---------------------------------------------------------------------------
# 安全
# ---------------------------------------------------------------------------
# 是否允许代理目标指向内网地址（localhost、私有 IP 等）。
# 默认允许，方便开发调试和代理内网服务。
# 如需部署到公网，建议设为 false 防止 SSRF 攻击。
ALLOW_INTERNAL_TARGETS = os.getenv("ALLOW_INTERNAL_TARGETS", "true").lower() == "true"

# ---------------------------------------------------------------------------
# 性能
# ---------------------------------------------------------------------------
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "40"))
DB_SAVE_WORKERS = int(os.getenv("DB_SAVE_WORKERS", "8"))

# 将独立字段导出，供 database.py 直接使用（不走 URL 解析）
_DB_USER_FOR_AUTO = _DB_USER
_DB_PASS_FOR_AUTO = _DB_PASS
_DB_HOST_FOR_AUTO = _DB_HOST
_DB_PORT_FOR_AUTO = int(_DB_PORT)
_DB_NAME_FOR_AUTO = _DB_NAME
