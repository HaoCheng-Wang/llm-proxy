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
        # override=False: 环境变量已设置的值优先于 .env 文件。
        # 这确保测试（通过 os.environ 设置 DATABASE_NAME=llm_proxy_test）、
        # Docker（通过 docker-compose environment 注入）等场景下，
        # .env 文件不会覆盖运行时注入的配置。
        load_dotenv(_p, override=False)
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
DISPLAY_IP = os.getenv("DISPLAY_IP", "your-server-ip")

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
# 用户注册审批
# ---------------------------------------------------------------------------
# 设为 true 时，新注册用户需管理员审批后才能登录使用。
# 设为 false（默认）时，新用户注册后可直接登录，无需审批。
REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "false").lower() == "true"

# ---------------------------------------------------------------------------
# 性能
# ---------------------------------------------------------------------------
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "40"))
DB_SAVE_WORKERS = int(os.getenv("DB_SAVE_WORKERS", "8"))

# 代理请求日志专用连接池（与 FastAPI 管理接口分离，避免互相影响）
DB_LOG_POOL_SIZE = int(os.getenv("DB_LOG_POOL_SIZE", "10"))
DB_LOG_MAX_OVERFLOW = int(os.getenv("DB_LOG_MAX_OVERFLOW", "20"))

# 端口→目标URL缓存TTL（秒），到期后下次查询从DB刷新
PORT_CACHE_TTL = int(os.getenv("PORT_CACHE_TTL", "5"))

# httpx keep-alive 空闲连接数。HTTP/1.1 下，请求完成后连接回到
# 池中保持温热，后续请求直接复用，省去 TLS 握手。
# max_connections 不设上限（httpx max_connections=None），由操作系统
# ulimit（文件描述符数量）自然限制。
HTTPX_MAX_KEEPALIVE_CONNECTIONS = int(os.getenv("HTTPX_MAX_KEEPALIVE_CONNECTIONS", "100"))

# 代理 请求体 / 响应体 的内存缓冲上限（字节）。
# 小于此值在内存中处理，超过则溢出到磁盘临时文件（SpooledTemporaryFile）。
# 流式和非流式路径均使用此缓冲区。
# 默认 10 MB — LLM API 请求/响应体通常远小于此值。
PROXY_BODY_MEMORY_LIMIT = int(os.getenv("PROXY_BODY_MEMORY_LIMIT", str(10 * 1024 * 1024)))


# 将独立字段导出，供 database.py 直接使用（不走 URL 解析）
_DB_USER_FOR_AUTO = _DB_USER
_DB_PASS_FOR_AUTO = _DB_PASS
_DB_HOST_FOR_AUTO = _DB_HOST
_DB_PORT_FOR_AUTO = int(_DB_PORT)
_DB_NAME_FOR_AUTO = _DB_NAME

# ── 后台清理批量参数 ──────────────────────────────
# 每次 DELETE 的行数上限，避免单条语句长时间锁表
CLEANUP_BATCH_SIZE = int(os.getenv("CLEANUP_BATCH_SIZE", "1000"))
# 每 N 批输出一次进度日志
CLEANUP_LOG_INTERVAL = int(os.getenv("CLEANUP_LOG_INTERVAL", "10"))

# ── 安全限制 ──────────────────────────────────────
# SSE 原始文本最大处理大小（字节）。超过此大小的 SSE 流将不会被重建为 JSON，
# 仅保存截断后的原始文本，防止超长流导致 Python 进程 OOM。
# 默认 50 MB — 对于典型 LLM 对话足够，极端情况下的超长流会被截断。
SSE_RECONSTRUCT_MAX_BYTES = int(os.getenv("SSE_RECONSTRUCT_MAX_BYTES", str(50 * 1024 * 1024)))

# DB 保存时单字段最大大小（字节）。超过此大小的 request_body/response_body
# 在写入数据库前会被截断并追加警告标记，防止单条 LONGTEXT 记录撑爆 MySQL
# 或导致 _sanitize_text() 在超大文本上消耗过多 CPU。
# 默认 100 MB，与 MySQL max_allowed_packet 64MB 留有安全余量。
DB_SAVE_FIELD_MAX_BYTES = int(os.getenv("DB_SAVE_FIELD_MAX_BYTES", str(100 * 1024 * 1024)))
