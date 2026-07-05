from concurrent.futures import ThreadPoolExecutor
import pymysql
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from config import (
    DATABASE_URL,
    DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_SAVE_WORKERS,
    DB_LOG_POOL_SIZE, DB_LOG_MAX_OVERFLOW,
    _DB_USER_FOR_AUTO, _DB_PASS_FOR_AUTO,
    _DB_HOST_FOR_AUTO, _DB_PORT_FOR_AUTO, _DB_NAME_FOR_AUTO,
)
import logging

Base = declarative_base()

engine = None
SessionLocal = None

# ── 代理请求日志专用连接池 ──────────────────────────────
# 与 FastAPI 管理接口的 engine 分离，避免大量流式请求结束时
# 的批量写库操作耗尽管理接口的可用连接。
_log_engine = None
LogSessionLocal = None

# Thread pool for non-blocking DB saves from proxy threads
_db_executor = ThreadPoolExecutor(max_workers=DB_SAVE_WORKERS, thread_name_prefix="db-save")

logger = logging.getLogger("llm_proxy.database")


def _ensure_database():
    """Connect to MySQL and create the database if it doesn't exist."""
    conn = pymysql.connect(
        host=_DB_HOST_FOR_AUTO,
        port=_DB_PORT_FOR_AUTO,
        user=_DB_USER_FOR_AUTO,
        password=_DB_PASS_FOR_AUTO,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{_DB_NAME_FOR_AUTO}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
        logger.info("Database '%s' is ready", _DB_NAME_FOR_AUTO)
    finally:
        conn.close()


def _init_engine():
    """Create the SQLAlchemy engine with connection pooling for concurrent access."""
    global engine, SessionLocal
    engine = create_engine(
        DATABASE_URL,
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=600,   # recycle before MySQL wait_timeout kills idle connections
        pool_timeout=30,
        connect_args={
            "connect_timeout": 10,
            "read_timeout": 60,
            "write_timeout": 60,
        },
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Dedicated session for streaming/large-result queries.
    # Uses pymysql SSCursor (server-side cursor) so rows are fetched incrementally
    # instead of loading the entire result set into memory.  A separate engine with
    # its own small pool prevents SSCursor connections from leaking into the normal
    # pool (SSCursor requires reading all rows before the next query).
    global _stream_engine, StreamSessionLocal
    _stream_engine = create_engine(
        DATABASE_URL,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=600,
        connect_args={
            "connect_timeout": 10,
            "read_timeout": 600,   # 10 minutes — enough for large result sets without filesort
            "write_timeout": 60,
            "cursorclass": pymysql.cursors.SSCursor,
        },
    )
    StreamSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_stream_engine)


def _init_log_engine():
    """Create a dedicated engine + sessionmaker for proxy request logging.

    This pool is independent from the FastAPI management pool so that a burst
    of log writes (e.g. 100 concurrent SSE streams finishing) never starves
    the management API of database connections.
    """
    global _log_engine, LogSessionLocal
    _log_engine = create_engine(
        DATABASE_URL,
        pool_size=DB_LOG_POOL_SIZE,
        max_overflow=DB_LOG_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=600,
        pool_timeout=30,
        connect_args={
            "connect_timeout": 10,
            "read_timeout": 60,
            "write_timeout": 60,
        },
    )
    LogSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_log_engine)
    logger.info("Log engine ready (pool_size=%d, max_overflow=%d)", DB_LOG_POOL_SIZE, DB_LOG_MAX_OVERFLOW)


def setup_schema():
    """Ensure database and all tables/columns/indexes exist.

    Called from ``main.py`` before ``uvicorn.run(…)``.
    """
    _ensure_database()
    _ddl_engine = create_engine(
        DATABASE_URL,
        connect_args={
            "connect_timeout": 10,
            "read_timeout": 60,
            "write_timeout": 60,
        },
    )
    try:
        import models  # noqa: F401
        Base.metadata.create_all(bind=_ddl_engine)
        logger.info("All tables verified")

        # Run column migration with the temp engine
        _migrate_columns_on_engine(_ddl_engine)
    finally:
        _ddl_engine.dispose()
        logger.info("Schema setup complete (DDL engine disposed)")


def _run_migration_ddl(conn, sql: str, success_msg: str):
    """Execute a migration DDL statement, logging success or the actual error."""
    try:
        conn.execute(text(sql))
        conn.commit()
        logger.info(success_msg)
    except Exception as e:
        conn.rollback()
        # Expected for "already exists" (1060=duplicate column, 1061=duplicate index),
        # but log anyway so the operator has full visibility.
        err_msg = str(e).rstrip()
        logger.warning("Migration skipped (%s): %s", sql[:60], err_msg[:200])


def _migrate_columns_on_engine(eng):
    """Like ``_migrate_columns()`` but accepts an explicit engine argument."""
    with eng.connect() as conn:
        # ── Column additions ──
        _run_migration_ddl(
            conn,
            "ALTER TABLE requests ADD COLUMN response_body_raw LONGTEXT NULL",
            "Added column: response_body_raw",
        )

        # Make port_id nullable so orphaned requests (from deleted ports)
        # can still be saved with raw response data intact.
        _run_migration_ddl(
            conn,
            "ALTER TABLE requests MODIFY COLUMN port_id INT NULL",
            "Altered port_id to nullable",
        )

        # Add reconstruction_error flag to requests for SSE failure tracking
        _run_migration_ddl(
            conn,
            "ALTER TABLE requests ADD COLUMN reconstruction_error TINYINT(1) NOT NULL DEFAULT 0",
            "Added column: requests.reconstruction_error",
        )

        # Add deleted_at column to ports for soft-delete
        _run_migration_ddl(
            conn,
            "ALTER TABLE ports ADD COLUMN deleted_at DATETIME NULL",
            "Added column: ports.deleted_at",
        )

        # Add prefer_http2 column to ports — NULL=HTTP/1.1 (default)
        _run_migration_ddl(
            conn,
            "ALTER TABLE ports ADD COLUMN prefer_http2 TINYINT(1) NULL",
            "Added column: ports.prefer_http2 (nullable)",
        )

        # Add api_key column to ports — NULL=pass-through, set=override agent's key
        _run_migration_ddl(
            conn,
            "ALTER TABLE ports ADD COLUMN api_key VARCHAR(500) NULL",
            "Added column: ports.api_key (nullable)",
        )

        for col in ["request_headers", "request_body", "response_headers",
                     "response_body", "response_body_raw"]:
            _run_migration_ddl(
                conn,
                f"ALTER TABLE requests MODIFY COLUMN `{col}` LONGTEXT NULL",
                f"Altered column type: {col} → LONGTEXT",
            )

        for idx_name, idx_sql in [
            ("ix_requests_port_id", "CREATE INDEX ix_requests_port_id ON requests (port_id)"),
            ("ix_requests_created_at", "CREATE INDEX ix_requests_created_at ON requests (created_at)"),
            ("ix_requests_port_created", "CREATE INDEX ix_requests_port_created ON requests (port_id, created_at DESC)"),
            ("ix_requests_port_method_created", "CREATE INDEX ix_requests_port_method_created ON requests (port_id, method, created_at)"),
        ]:
            _run_migration_ddl(conn, idx_sql, f"Added index: {idx_name}")

        logger.info("Column migration complete")


def init_database():
    """Startup: create connection pools."""
    _init_engine()
    _init_log_engine()
    logger.info("Engine ready (pool_size=%d, max_overflow=%d)", DB_POOL_SIZE, DB_MAX_OVERFLOW)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def shutdown_db_executor():
    """Gracefully shut down the dedicated DB thread pool."""
    if _db_executor:
        _db_executor.shutdown(wait=True)
        logger.info("DB executor thread pool shut down")
