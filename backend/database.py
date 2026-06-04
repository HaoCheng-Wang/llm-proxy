from concurrent.futures import ThreadPoolExecutor
import pymysql
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import (
    DATABASE_URL,
    DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_SAVE_WORKERS,
    _DB_USER_FOR_AUTO, _DB_PASS_FOR_AUTO,
    _DB_HOST_FOR_AUTO, _DB_PORT_FOR_AUTO, _DB_NAME_FOR_AUTO,
)

Base = declarative_base()

engine = None
SessionLocal = None

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
            "read_timeout": 30,
            "write_timeout": 30,
        },
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _migrate_columns():
    """Ensure request storage columns use LONGTEXT; add missing columns and indexes."""
    if engine is None:
        return
    with engine.connect() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE requests ADD COLUMN response_body_raw LONGTEXT NULL"
            ))
            conn.commit()
            print("[DB] Added column: response_body_raw")
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

        # Add indexes for faster queries (idempotent - ignore if already exists)
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
    """Full initialization: DB → tables → migrations."""
    _ensure_database()
    _init_engine()
    import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    print(f"[DB] All tables verified (pool_size={DB_POOL_SIZE}, max_overflow={DB_MAX_OVERFLOW})")
    _migrate_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_in_db_executor(fn, *args):
    """Run a DB operation in the shared thread pool (non-blocking)."""
    return _db_executor.submit(fn, *args)
