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
        print(f"[DB] Database '{_DB_NAME_FOR_AUTO}' is ready")
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
    print(f"[DB] Log engine ready (pool_size={DB_LOG_POOL_SIZE}, max_overflow={DB_LOG_MAX_OVERFLOW})")


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
        print("[DB] All tables verified")

        # Run column migration with the temp engine
        _migrate_columns_on_engine(_ddl_engine)
    finally:
        _ddl_engine.dispose()
        print("[DB] Schema setup complete (DDL engine disposed)")


def _migrate_columns_on_engine(eng):
    """Like ``_migrate_columns()`` but accepts an explicit engine argument."""
    with eng.connect() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE requests ADD COLUMN response_body_raw LONGTEXT NULL"
            ))
            conn.commit()
            print("[DB] Added column: response_body_raw")
        except Exception:
            conn.rollback()

        # Make port_id nullable so orphaned requests (from deleted ports)
        # can still be saved with raw response data intact.
        try:
            conn.execute(text(
                "ALTER TABLE requests MODIFY COLUMN port_id INT NULL"
            ))
            conn.commit()
            print("[DB] Altered port_id to nullable")
        except Exception:
            conn.rollback()

        # Add reconstruction_error flag to requests for SSE failure tracking
        try:
            conn.execute(text(
                "ALTER TABLE requests ADD COLUMN reconstruction_error TINYINT(1) "
                "NOT NULL DEFAULT 0"
            ))
            conn.commit()
            print("[DB] Added column: requests.reconstruction_error")
        except Exception:
            conn.rollback()

        # Add deleted_at column to ports for soft-delete
        try:
            conn.execute(text(
                "ALTER TABLE ports ADD COLUMN deleted_at DATETIME NULL"
            ))
            conn.commit()
            print("[DB] Added column: ports.deleted_at")
        except Exception:
            conn.rollback()

        for col in ["request_headers", "request_body", "response_headers",
                     "response_body", "response_body_raw"]:
            try:
                conn.execute(
                    text(f"ALTER TABLE requests MODIFY COLUMN `{col}` LONGTEXT NULL")
                )
                conn.commit()
            except Exception:
                conn.rollback()

        for idx_name, idx_sql in [
            ("ix_requests_port_id", "CREATE INDEX ix_requests_port_id ON requests (port_id)"),
            ("ix_requests_created_at", "CREATE INDEX ix_requests_created_at ON requests (created_at)"),

        ]:
            try:
                conn.execute(text(idx_sql))
                conn.commit()
                print(f"[DB] Added index: {idx_name}")
            except Exception:
                conn.rollback()

        print("[DB] Column migration complete")


def init_database():
    """Startup: create connection pools."""
    _init_engine()
    _init_log_engine()
    print(f"[DB] Engine ready (pool_size={DB_POOL_SIZE}, max_overflow={DB_MAX_OVERFLOW})")


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
        print("[DB] DB executor thread pool shut down")
