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
        autocommit=True,
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
            "read_timeout": 300,
            "write_timeout": 300,
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
            "read_timeout": 300,
            "write_timeout": 300,
        },
    )
    try:
        import models  # noqa: F401
        Base.metadata.create_all(bind=_ddl_engine)
        logger.info("All tables verified")

        # Run column migration with the temp engine
        _migrate_columns_on_engine(_ddl_engine)

        # Set up MySQL Event for guaranteed background port cleanup
        _setup_cleanup_event(_ddl_engine)
    finally:
        _ddl_engine.dispose()
        logger.info("Schema setup complete (DDL engine disposed)")


def _setup_cleanup_event(eng):
    """Create a MySQL Event that periodically nibble-deletes requests
    for ports flagged with ``cleaning_started_at``.

    This is the *guaranteed* cleanup channel: even if the Python process
    dies and never restarts, the MySQL server itself continues cleaning
    up flagged ports at 30-second intervals.
    """
    try:
        with eng.connect() as conn:
            row = conn.execute(text("SELECT @@event_scheduler")).first()
            if not row or row[0] not in ("ON", "1"):
                logger.warning(
                    "MySQL event_scheduler is %s — background cleanup will "
                    "rely on the application's daemon threads. Enable with: "
                    "SET GLOBAL event_scheduler = ON;",
                    row[0] if row else "unknown",
                )
                return

            conn.execute(text("""
                CREATE EVENT IF NOT EXISTS evt_cleanup_flagged_ports
                ON SCHEDULE EVERY 30 SECOND
                STARTS CURRENT_TIMESTAMP
                ON COMPLETION PRESERVE
                COMMENT 'Nibble-delete requests for ports flagged for permanent deletion'
                DO
                BEGIN
                    DECLARE v_port_id INT;
                    DECLARE v_affected INT DEFAULT 0;
                    DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_port_id = NULL;

                    SELECT id INTO v_port_id FROM ports
                    WHERE cleaning_started_at IS NOT NULL
                    ORDER BY cleaning_started_at LIMIT 1;

                    IF v_port_id IS NOT NULL THEN
                        DELETE FROM requests WHERE port_id = v_port_id LIMIT 1000;
                        SET v_affected = ROW_COUNT();
                        IF v_affected = 0 THEN
                            DELETE FROM ports WHERE id = v_port_id;
                        END IF;
                    END IF;
                END
            """))
            conn.commit()
            logger.info("MySQL cleanup event 'evt_cleanup_flagged_ports' ready (every 30s)")
    except Exception:
        logger.warning("Could not create MySQL cleanup event (non-fatal)", exc_info=True)


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


def _column_exists(conn, table: str, column: str) -> bool:
    """Check whether *column* already exists in *table*."""
    row = conn.execute(
        text(
            "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).first()
    return row is not None


def _column_is_nullable(conn, table: str, column: str) -> bool | None:
    """Return True/False if *column* is nullable, or None if column not found."""
    row = conn.execute(
        text(
            "SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).first()
    if row is None:
        return None
    return row[0] == "YES"


def _column_data_type(conn, table: str, column: str) -> str | None:
    """Return COLUMN_TYPE (e.g. 'int', 'longtext', 'varchar(500)') or None."""
    row = conn.execute(
        text(
            "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).first()
    if row is None:
        return None
    return row[0].lower()


def _add_column_if_missing(conn, table: str, column: str, definition: str):
    """ADD COLUMN only if the column does not already exist."""
    if _column_exists(conn, table, column):
        return
    _run_migration_ddl(
        conn,
        f"ALTER TABLE {table} ADD COLUMN {column} {definition}",
        f"Added column: {table}.{column}",
    )


def _modify_column_if_needed(conn, table: str, column: str, definition: str):
    """MODIFY COLUMN only if its current definition differs.

    *definition* is the full MySQL column type, e.g. 'LONGTEXT NULL'.
    We compare COLUMN_TYPE from INFORMATION_SCHEMA (e.g. 'longtext').
    """
    current = _column_data_type(conn, table, column)
    if current is None:
        return  # column doesn't exist — nothing to modify
    expected = definition.lower().strip()
    if current == expected.split(" ")[0]:  # compare just the type, e.g. 'longtext'
        # Also check nullability for NULL / NOT NULL changes
        if " null" in expected or expected.endswith(" null"):
            if not _column_is_nullable(conn, table, column):
                pass  # fall through to modify
            else:
                return  # already nullable, skip
    _run_migration_ddl(
        conn,
        f"ALTER TABLE {table} MODIFY COLUMN `{column}` {definition}",
        f"Altered column: {table}.{column} → {definition}",
    )


def _modify_nullable_if_needed(conn, table: str, column: str, definition: str):
    """MODIFY COLUMN to change nullability, but only if different."""
    current_nullable = _column_is_nullable(conn, table, column)
    if current_nullable is None:
        return
    wants_nullable = "null" in definition.lower()
    if current_nullable == wants_nullable:
        return  # already in desired state
    _run_migration_ddl(
        conn,
        f"ALTER TABLE {table} MODIFY COLUMN `{column}` {definition}",
        f"Altered nullability: {table}.{column} → {definition}",
    )


def _index_exists(conn, index_name: str) -> bool:
    """Check whether *index_name* already exists in the current database."""
    row = conn.execute(
        text(
            "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND INDEX_NAME = :idx LIMIT 1"
        ),
        {"idx": index_name},
    ).first()
    return row is not None


def _migrate_columns_on_engine(eng):
    """Check INFORMATION_SCHEMA before each DDL to avoid expensive ALTER TABLE
    on tables with millions of rows when the migration has already been applied."""
    with eng.connect() as conn:
        # ── Column additions (only if missing) ──
        _add_column_if_missing(conn, "requests", "response_body_raw", "LONGTEXT NULL")
        _add_column_if_missing(conn, "requests", "reconstruction_error", "TINYINT(1) NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "ports", "deleted_at", "DATETIME NULL")
        _add_column_if_missing(conn, "ports", "prefer_http2", "TINYINT(1) NULL")
        _add_column_if_missing(conn, "ports", "api_key", "VARCHAR(500) NULL")
        _add_column_if_missing(conn, "ports", "cleaning_started_at", "DATETIME NULL")

        # ── Nullability changes (only if different) ──
        _modify_nullable_if_needed(conn, "requests", "port_id", "INT NULL")

        # ── Type changes (only if different) ──
        for col in ["request_headers", "request_body", "response_headers",
                     "response_body", "response_body_raw"]:
            _modify_column_if_needed(conn, "requests", col, "LONGTEXT NULL")

        # ── Index creation (only if missing) ──
        for idx_name, idx_sql in [
            ("ix_requests_port_id", "CREATE INDEX ix_requests_port_id ON requests (port_id)"),
            ("ix_requests_created_at", "CREATE INDEX ix_requests_created_at ON requests (created_at)"),
            ("ix_requests_port_created", "CREATE INDEX ix_requests_port_created ON requests (port_id, created_at DESC)"),
            ("ix_requests_port_method_created", "CREATE INDEX ix_requests_port_method_created ON requests (port_id, method, created_at)"),
        ]:
            if _index_exists(conn, idx_name):
                continue
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


def shutdown_db_executor(timeout: float = 15.0):
    """Gracefully shut down the dedicated DB thread pool.

    ``wait=True`` is used but with a timeout via a daemon thread watchdog:
    if pending tasks have not completed within *timeout* seconds, the
    executor is forcibly abandoned.  This prevents a stuck DB-save task
    (e.g. MySQL connection hangs) from blocking process shutdown
    indefinitely — which would make ``kill $(cat back.pid)`` ineffective.
    """
    import threading as _threading
    if not _db_executor:
        return
    cancelled = False

    def _force_cancel():
        nonlocal cancelled
        cancelled = True
        logger.warning(
            "DB executor shutdown timed out after %.1fs — "
            "forcing shutdown (pending tasks will be abandoned)",
            timeout,
        )
        _db_executor.shutdown(wait=False)

    watchdog = _threading.Timer(timeout, _force_cancel)
    watchdog.daemon = True
    watchdog.start()
    try:
        _db_executor.shutdown(wait=True)
    finally:
        watchdog.cancel()
    if not cancelled:
        logger.info("DB executor thread pool shut down")


# ── Shared cleanup utilities (used by admin_router + ports_router) ──

def get_raw_connection():
    """Create a dedicated pymysql connection with 1-hour timeouts.

    Used by background cleanup threads so they do not compete with the
    FastAPI connection pool.  Each thread owns its connection for its
    entire lifetime.
    """
    import pymysql
    from config import (
        _DB_USER_FOR_AUTO, _DB_PASS_FOR_AUTO,
        _DB_HOST_FOR_AUTO, _DB_PORT_FOR_AUTO, _DB_NAME_FOR_AUTO,
    )
    return pymysql.connect(
        host=_DB_HOST_FOR_AUTO, port=_DB_PORT_FOR_AUTO,
        user=_DB_USER_FOR_AUTO, password=_DB_PASS_FOR_AUTO,
        database=_DB_NAME_FOR_AUTO, charset="utf8mb4",
        read_timeout=3600, write_timeout=3600, connect_timeout=10,
    )


def warn_fragmented_raw(raw_conn):
    """Log a warning if the requests table has significant fragmentation.

    Best-effort — never raises.  Called after large batch-deletes to
    remind the operator about ``OPTIMIZE TABLE requests;``.
    """
    try:
        with raw_conn.cursor() as cur:
            cur.execute(
                "SELECT data_free, data_length + index_length "
                "FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = 'requests'",
                (raw_conn.db.decode() if isinstance(raw_conn.db, bytes) else raw_conn.db,),
            )
            row = cur.fetchone()
        if row is None:
            return
        data_free, total = row[0] or 0, row[1] or 1
        if total > 0 and data_free / total > 0.3:
            logger.warning(
                "requests table is %.0f%% fragmented (%s MB wasted). "
                "Consider running: OPTIMIZE TABLE requests; "
                "during a maintenance window to reclaim disk space.",
                data_free / total * 100,
                round(data_free / 1024 / 1024, 1),
            )
    except Exception:
        logger.warning("Failed to check table fragmentation (non-critical)", exc_info=True)
