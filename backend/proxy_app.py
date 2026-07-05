"""
Proxy application — a Starlette ASGI app that intercepts requests,
forwards them to the target URL, and records everything to the database.

Supports both regular JSON responses and streaming (SSE) responses.
"""
from __future__ import annotations
import time
import json
import asyncio
import logging
import httpx
import certifi
import database
from models import Port, Request as RequestModel
from config import PORT_CACHE_TTL, HTTPX_MAX_KEEPALIVE_CONNECTIONS

logger = logging.getLogger("llm_proxy.proxy")


def _sanitize_text(value: str | None) -> str | None:
    """Remove lone surrogate characters that MySQL's utf8mb4 cannot store."""
    if value is None:
        return None
    try:
        # "strict" mode raises UnicodeEncodeError for surrogates.
        # "surrogatepass" would allow them through — don't use that here.
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        # Count surrogates for a concise single-line warning
        surrogate_count = sum(
            1 for ch in value if "\uD800" <= ch <= "\uDFFF"
        )
        cleaned = value.encode("utf-8", errors="replace").decode("utf-8")
        logger.warning(
            "Replaced %d surrogate(s), %d → %d chars",
            surrogate_count, len(value), len(cleaned),
        )
        return cleaned


# ──────────────────────────────────────────────
#  Background task tracking — prevent GC of save tasks
# ──────────────────────────────────────────────
# asyncio.create_task() only holds a *weak* reference to the task.  If the
# caller doesn't save the returned Task object, Python's garbage collector
# can collect it before it runs, silently losing the database write.
# This is a well-documented gotcha:
#   https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_pending_save_tasks: set[asyncio.Task] = set()


def _fire_and_forget_save(coro):
    """Schedule a background DB-save task and prevent garbage collection.

    The task is tracked in ``_pending_save_tasks``; a done-callback removes
    it and logs any unhandled exception so failures appear in docker logs
    instead of being silently swallowed.
    """
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running event loop (e.g. during shutdown) — can't schedule
        logger.warning("No running event loop — background save skipped")
        return None
    _pending_save_tasks.add(task)

    def _on_done(t: asyncio.Task):
        _pending_save_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "Background save task failed: %s: %s",
                type(exc).__name__, exc,
                exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task


async def drain_pending_saves(timeout: float = 10.0):
    """Wait for all pending background save tasks to complete.

    Called during graceful shutdown to ensure no records are lost when
    the database engine and thread pool are disposed.  A timeout prevents
    the server from hanging indefinitely if a save is stuck.
    """
    if not _pending_save_tasks:
        return
    logger.info("Draining %d pending save task(s)...", len(_pending_save_tasks))
    try:
        await asyncio.wait_for(
            asyncio.gather(*_pending_save_tasks, return_exceptions=True),
            timeout=timeout,
        )
        logger.info("All pending save tasks completed")
    except asyncio.TimeoutError:
        logger.warning(
            "Drain timed out after %.1fs — %d task(s) still pending",
            timeout, len(_pending_save_tasks),
        )


# Shared httpx clients — HTTP/1.1 (default, stable) and HTTP/2 (opt-in per port).
# HTTP/2 multiplexing causes GOAWAY races: upstream APIs (especially relays like
# dmxapi.cn) periodically recycle idle connections, and a mid-stream GOAWAY
# kills the response.  HTTP/1.1 is request-per-connection — no GOAWAY.
# Users can opt into HTTP/2 per-port in the frontend when they trust the target.
_shared_client: httpx.AsyncClient | None = None  # HTTP/1.1
_http2_client: httpx.AsyncClient | None = None   # HTTP/2


def init_shared_client() -> httpx.AsyncClient:
    """Create (or return existing) HTTP/1.1 client.  Call at startup."""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=15.0, read=120.0),
            limits=httpx.Limits(
                max_connections=None,
                max_keepalive_connections=HTTPX_MAX_KEEPALIVE_CONNECTIONS,
            ),
            follow_redirects=False,
            verify=certifi.where(),
            http2=False,
        )
        logger.info(
            "HTTP/1.1 client ready (max_connections=unlimited, "
            "keepalive=%d, read_timeout=120s)",
            HTTPX_MAX_KEEPALIVE_CONNECTIONS,
        )
    return _shared_client


def get_shared_client() -> httpx.AsyncClient:
    """Get the HTTP/1.1 shared client."""
    global _shared_client
    if _shared_client is None:
        init_shared_client()
    return _shared_client


async def close_shared_client():
    global _shared_client
    if _shared_client:
        await _shared_client.aclose()
        _shared_client = None


def init_http2_client() -> httpx.AsyncClient | None:
    """Create (or return existing) HTTP/2 client.  Call at startup.

    Returns None if h2 is not installed — the caller (get_http2_client,
    shared_proxy) falls back to HTTP/1.1 automatically.
    """
    global _http2_client
    if _http2_client is None:
        try:
            import h2  # noqa: F401
        except ImportError:
            logger.warning(
                "h2 not installed — HTTP/2 unavailable. "
                "Ports with prefer_http2=True will fall back to HTTP/1.1. "
                "Install with: pip install httpx[http2]",
            )
            _http2_client = None  # sentinel: tried but failed
            return None
        _http2_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=15.0, read=300.0),
            limits=httpx.Limits(
                max_connections=None,
                max_keepalive_connections=HTTPX_MAX_KEEPALIVE_CONNECTIONS,
            ),
            follow_redirects=False,
            verify=certifi.where(),
            http2=True,
        )
        logger.info(
            "HTTP/2 client ready (max_connections=unlimited, "
            "keepalive=%d)",
            HTTPX_MAX_KEEPALIVE_CONNECTIONS,
        )
    return _http2_client


def get_http2_client() -> httpx.AsyncClient | None:
    """Get the HTTP/2 shared client.  Returns None if unavailable (no h2)."""
    global _http2_client
    if _http2_client is None:
        return init_http2_client()
    return _http2_client


async def close_http2_client():
    global _http2_client
    if _http2_client:
        await _http2_client.aclose()
        _http2_client = None


# In-memory cache of port_number → (target_url, prefer_http2) mappings.
# Refreshed from DB on startup and after PORT_CACHE_TTL seconds.
_port_target_cache: dict[int, tuple[str, bool | None]] = {}
_cache_updated_at: float = 0.0


def refresh_port_cache(db=None):
    """Refresh the port_number → (target_url, prefer_http2) cache from DB."""
    global _port_target_cache, _cache_updated_at
    if db is None:
        db = database.SessionLocal()
        try:
            ports = db.query(Port).filter(
                Port.is_active.is_(True), Port.deleted_at.is_(None)
            ).all()
            _port_target_cache = {
                p.port_number: (p.target_url, p.prefer_http2) for p in ports
            }
        finally:
            db.close()
    else:
        ports = db.query(Port).filter(
            Port.is_active.is_(True), Port.deleted_at.is_(None)
        ).all()
        _port_target_cache = {
            p.port_number: (p.target_url, p.prefer_http2) for p in ports
        }
    _cache_updated_at = time.time()


def _maybe_refresh_cache():
    """Refresh cache from DB if TTL has expired.

    Safe to call from the event loop (never from a thread-pool thread).
    """
    if time.time() - _cache_updated_at > PORT_CACHE_TTL:
        refresh_port_cache()


def get_target_url(port_number: int) -> tuple[str, bool | None] | None:
    """Get (target_url, prefer_http2) for a port. Falls back to DB.

    WARNING: This is a SYNC function — use ``aget_target_url()`` from async
    contexts to avoid blocking the event loop.
    """
    _maybe_refresh_cache()
    cache = _port_target_cache
    if port_number in cache:
        return cache[port_number]

    # Cache miss — query DB and update cache
    db = database.SessionLocal()
    try:
        port = db.query(Port).filter(
            Port.port_number == port_number,
            Port.is_active.is_(True),
            Port.deleted_at.is_(None),
        ).first()
        if port:
            entry = (port.target_url, port.prefer_http2)
            _port_target_cache[port.port_number] = entry
            return entry
        return None
    finally:
        db.close()


async def _arefresh_port_cache():
    """Async wrapper: refresh the port→target_url cache in a thread.

    Avoids blocking the asyncio event loop when called from async handlers.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, refresh_port_cache)


async def aget_target_url(port_number: int) -> tuple[str, bool | None] | None:
    """Async: get (target_url, prefer_http2) for a port. Runs DB in thread pool."""
    if time.time() - _cache_updated_at <= PORT_CACHE_TTL:
        cache = _port_target_cache
        if port_number in cache:
            return cache[port_number]
    else:
        await _arefresh_port_cache()
        cache = _port_target_cache
        if port_number in cache:
            return cache[port_number]

    loop = asyncio.get_running_loop()

    def _db_lookup():
        db = database.SessionLocal()
        try:
            port = db.query(Port).filter(
                Port.port_number == port_number,
                Port.is_active.is_(True),
                Port.deleted_at.is_(None),
            ).first()
            if port:
                entry = (port.target_url, port.prefer_http2)
                _port_target_cache[port.port_number] = entry
                return entry
            return None
        finally:
            db.close()

    return await loop.run_in_executor(None, _db_lookup)


def _save_to_db(port_number: int, method: str, path: str,
                req_headers: str, req_body: str | None,
                resp_headers: str, resp_body: str | None,
                status_code: int, duration_ms: int,
                resp_body_raw: str | None = None,
                reconstruction_error: bool = False):
    """Save a request/response record to the database. Runs in a thread.
    Retries up to 3 times on transient connection errors.

    Uses the dedicated log engine (LogSessionLocal) so that burst writes from
    proxy logging never compete with FastAPI management API connections.
    """
    last_error = None
    for attempt in range(3):
        db = database.LogSessionLocal()
        try:
            port = db.query(Port).filter(
                Port.port_number == port_number,
                Port.is_active.is_(True),
                Port.deleted_at.is_(None),
            ).first()
            port_id = port.id if port else None
            if not port_id:
                logger.warning(
                    "port %d not active — saving record with port_id=NULL",
                    port_number,
                )

            record = RequestModel(
                port_id=port_id,
                method=method,
                path=path,
                request_headers=_sanitize_text(req_headers),
                request_body=_sanitize_text(req_body),
                response_headers=_sanitize_text(resp_headers),
                response_body=_sanitize_text(resp_body),
                response_body_raw=_sanitize_text(resp_body_raw),
                status_code=status_code,
                duration_ms=duration_ms,
                reconstruction_error=reconstruction_error,
            )
            db.add(record)
            db.commit()
            return  # success
        except Exception as e:
            db.rollback()
            last_error = e
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))  # 0.5s, 1.0s backoff
        finally:
            db.close()

    logger.error(
        "ERROR saving request record for port %d after 3 retries",
        port_number,
        exc_info=(type(last_error), last_error, last_error.__traceback__),
    )


async def _save_record_async(port_number: int, method: str, path: str,
                              req_headers: str, req_body: str | None,
                              resp_headers: str, resp_body: str | None,
                              status_code: int, duration_ms: int,
                              resp_body_raw: str | None = None,
                              reconstruction_error: bool = False):
    """Async wrapper — runs the sync DB save in the dedicated log thread pool.

    Uses ``database._db_executor`` (configured via DB_SAVE_WORKERS) so that
    burst log writes never compete with asyncio's default thread pool.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        database._db_executor,
        _save_to_db,
        port_number, method, path,
        req_headers, req_body,
        resp_headers, resp_body,
        status_code, duration_ms,
        resp_body_raw,
        reconstruction_error,
    )


# Headers to exclude when forwarding
EXCLUDE_HEADERS = {
    "host", "content-length", "connection", "transfer-encoding",
    "content-encoding",   # httpx decompresses for us
    "accept-encoding",    # avoid upstream returning compressed content
}

# Symbols exported for use by shared_proxy and main
__all__ = [
    "init_shared_client",
    "get_shared_client",
    "close_shared_client",
    "init_http2_client",
    "get_http2_client",
    "close_http2_client",
    "refresh_port_cache",
    "get_target_url",
    "aget_target_url",
    "_serialize_body",
    "_save_to_db",
    "_save_record_async",
    "_sanitize_text",
    "_fire_and_forget_save",
    "drain_pending_saves",
    "_reconstruct_sse_to_json",
    "EXCLUDE_HEADERS",
]


def _serialize_body(body_bytes: bytes, label: str = "body") -> str | None:
    """Convert raw body bytes to a pretty-printed string.

    Args:
        body_bytes: Raw bytes to serialize
        label: Label for logging (e.g., "request", "response")
    """
    if not body_bytes:
        return None
    try:
        text = body_bytes.decode("utf-8")
        try:
            parsed = json.loads(text)
            result = json.dumps(parsed, ensure_ascii=False, indent=2)
            # The decoded text may contain surrogates left by surrogateescape
            # during stream reads.  Sanitize before handing off to DB writers.
            try:
                result.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                logger.warning(
                    "%s body contains surrogate characters after JSON "
                    "serialization — sanitizing",
                    label,
                )
                result = result.encode("utf-8", errors="replace").decode("utf-8")
            return result
        except (json.JSONDecodeError, Exception) as e:
            logger.debug(
                "Body is not JSON, storing as-is (%s): %s",
                label, str(e)[:120],
            )
            return text
    except UnicodeDecodeError:
        logger.warning(
            "request body is not valid UTF-8 — storing as binary "
            "placeholder (%d bytes)",
            len(body_bytes),
        )
        return f"[binary data, {len(body_bytes)} bytes]"
# ──────────────────────────────────────────────
#  SSE parsing — delegated to sse_parsers module
# ──────────────────────────────────────────────

import sse_parsers  # noqa: E402 (import after helpers for readability)

from sse_parsers import (  # noqa: E402
    detect_sse_format as _detect_sse_format,
    reconstruct_sse_to_json as _reconstruct_sse_to_json,
    deep_merge as _deep_merge,
)

# Backward-compatible function references for tests and internal use.
# Each parser class has a .parse() classmethod matching the old function signature.
_parse_anthropic_sse = sse_parsers.AnthropicSSEParser.parse
_parse_openai_chat_sse = sse_parsers.OpenAIChatSSEParser.parse
_parse_openai_responses_sse = sse_parsers.OpenAIResponsesSSEParser.parse
_parse_gemini_sse = sse_parsers.GeminiSSEParser.parse


# NOTE: The old per-port proxy endpoint (proxy_endpoint / create_proxy_app)
# was removed in favour of the shared-proxy architecture in shared_proxy.py.
# All proxy traffic now flows through shared_proxy_endpoint().
