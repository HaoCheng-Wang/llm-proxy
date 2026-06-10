"""
Proxy application — a Starlette ASGI app that intercepts requests,
forwards them to the target URL, and records everything to the database.

Supports both regular JSON responses and streaming (SSE) responses.
"""
from __future__ import annotations
import time
import json
import sys
import traceback
import asyncio
import httpx
import certifi
import database
from models import Port, Request as RequestModel
from config import PORT_CACHE_TTL, HTTPX_MAX_CONNECTIONS, HTTPX_MAX_KEEPALIVE_CONNECTIONS


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
        print(
            f"[Sanitize] Replaced {surrogate_count} surrogate(s), "
            f"{len(value)} → {len(cleaned)} chars",
            file=sys.stderr,
        )
        return cleaned


# Shared httpx client with connection pooling — reused across all proxy requests.
# Created eagerly at startup (via init_shared_client()) so the first proxy
# request doesn't pay lazy-init overhead (certifi loading, etc.).
_shared_client: httpx.AsyncClient | None = None

# Separate HTTP/1.1-only client for streaming requests.
# HTTP/2 connection pooling suffers from GOAWAY races: upstream LLM APIs
# periodically recycle idle connections, and a mid-stream GOAWAY kills
# the response with no way to retry (data already sent to client).
# HTTP/1.1 is request-per-connection — no multiplexing, no GOAWAY.
_streaming_client: httpx.AsyncClient | None = None


def init_shared_client() -> httpx.AsyncClient:
    """Create (or return existing) shared httpx client.  Call at startup."""
    global _shared_client
    if _shared_client is None:
        # Try HTTP/2 first; fall back to HTTP/1.1 if h2 is not installed.
        http2 = True
        try:
            import h2  # noqa: F401
        except ImportError:
            http2 = False
            print(
                "[Proxy] h2 not installed — using HTTP/1.1. "
                "Install with: pip install httpx[http2]",
                file=sys.stderr,
            )

        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=15.0),
            limits=httpx.Limits(
                max_connections=HTTPX_MAX_CONNECTIONS,
                max_keepalive_connections=HTTPX_MAX_KEEPALIVE_CONNECTIONS,
            ),
            follow_redirects=False,
            verify=certifi.where(),
            http2=http2,
        )
    return _shared_client


def get_shared_client() -> httpx.AsyncClient:
    """Get the shared httpx client (must be initialized at startup)."""
    global _shared_client
    if _shared_client is None:
        # Defensive fallback — should not happen in normal operation
        init_shared_client()
    return _shared_client


async def close_shared_client():
    global _shared_client
    if _shared_client:
        await _shared_client.aclose()
        _shared_client = None


def init_streaming_client() -> httpx.AsyncClient:
    """Create (or return) HTTP/1.1-only client for streaming requests.

    Why HTTP/1.1: LLM streaming (SSE) is a long-lived response.  Under
    HTTP/2, the upstream may send GOAWAY mid-stream when recycling idle
    connections — killing the response with no way to retry because data
    has already been forwarded to the client.

    HTTP/1.1 is request-per-connection: each stream owns its TCP socket.
    No multiplexing means no GOAWAY.  The upstream can close the
    connection after the response completes, but never during.
    """
    global _streaming_client
    if _streaming_client is None:
        _streaming_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=15.0, read=120.0),
            limits=httpx.Limits(
                max_connections=HTTPX_MAX_CONNECTIONS,
                max_keepalive_connections=0,  # no pooling — one conn per stream
            ),
            follow_redirects=False,
            verify=certifi.where(),
            http2=False,  # force HTTP/1.1
        )
        print(
            "[Proxy] Streaming client ready (HTTP/1.1, keepalive=0, "
            "read_timeout=120s)",
            file=sys.stderr,
        )
    return _streaming_client


def get_streaming_client() -> httpx.AsyncClient:
    """Get the HTTP/1.1 streaming client (must be initialized at startup)."""
    global _streaming_client
    if _streaming_client is None:
        init_streaming_client()
    return _streaming_client


async def close_streaming_client():
    global _streaming_client
    if _streaming_client:
        await _streaming_client.aclose()
        _streaming_client = None


# In-memory cache of port_number → target_url mappings.
# Refreshed from DB on startup and after PORT_CACHE_TTL seconds.
_port_target_cache: dict[int, str] = {}
_cache_updated_at: float = 0.0


def refresh_port_cache(db=None):
    """Refresh the port -> target_url cache from database."""
    global _port_target_cache, _cache_updated_at
    if db is None:
        db = database.SessionLocal()
        try:
            ports = db.query(Port).filter(
                Port.is_active.is_(True), Port.deleted_at.is_(None)
            ).all()
            _port_target_cache = {p.port_number: p.target_url for p in ports}
        finally:
            db.close()
    else:
        ports = db.query(Port).filter(
            Port.is_active.is_(True), Port.deleted_at.is_(None)
        ).all()
        _port_target_cache = {p.port_number: p.target_url for p in ports}
    _cache_updated_at = time.time()


def _maybe_refresh_cache():
    """Refresh cache from DB if TTL has expired.

    Safe to call from the event loop (never from a thread-pool thread).
    """
    if time.time() - _cache_updated_at > PORT_CACHE_TTL:
        refresh_port_cache()


def get_target_url(port_number: int) -> str | None:
    """Get target URL for a port. Auto-refreshes stale cache, falls back to DB.

    WARNING: This is a SYNC function — it may perform blocking DB queries.
    Use ``aget_target_url()`` from async contexts to avoid blocking the event loop.
    """
    # Check cache first (with TTL auto-refresh).
    # Capture the dict reference ONCE to avoid a TOCTOU race where
    # refresh_port_cache (running in a thread) replaces the dict between
    # the membership check and the subscript lookup.
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
            _port_target_cache[port.port_number] = port.target_url
            return port.target_url
        return None
    finally:
        db.close()


async def _arefresh_port_cache():
    """Async wrapper: refresh the port→target_url cache in a thread.

    Avoids blocking the asyncio event loop when called from async handlers.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, refresh_port_cache)


async def aget_target_url(port_number: int) -> str | None:
    """Async version of ``get_target_url`` — runs DB queries in a thread pool.

    This is the function to use from async contexts (e.g. FastAPI route handlers)
    to avoid blocking the asyncio event loop on synchronous DB operations.
    """
    # ── Fast path: cache hit ──
    # Capture the dict reference ONCE to avoid a TOCTOU race where
    # refresh_port_cache (running in a thread via _arefresh_port_cache)
    # replaces the dict between the membership check and the subscript lookup.
    if time.time() - _cache_updated_at <= PORT_CACHE_TTL:
        cache = _port_target_cache
        if port_number in cache:
            return cache[port_number]
    else:
        # Cache TTL expired — refresh async (DB query in thread)
        await _arefresh_port_cache()
        cache = _port_target_cache
        if port_number in cache:
            return cache[port_number]

    # ── Slow path: cache miss — async DB lookup ──
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
                # Update cache so subsequent lookups are fast
                _port_target_cache[port.port_number] = port.target_url
                return port.target_url
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
    import time as _time
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
                print(
                    f"[Proxy] WARNING: port {port_number} not active — "
                    f"saving record with port_id=NULL",
                    file=sys.stderr,
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
                _time.sleep(0.5 * (attempt + 1))  # 0.5s, 1.0s backoff
        finally:
            db.close()

    print(f"[Proxy] ERROR saving request record for port {port_number} after 3 retries:",
          file=sys.stderr)
    traceback.print_exception(type(last_error), last_error, last_error.__traceback__,
                              file=sys.stderr)


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
    "content-encoding",  # httpx decompresses for us
}

# Symbols exported for use by shared_proxy and main
__all__ = [
    "init_shared_client",
    "get_shared_client",
    "close_shared_client",
    "init_streaming_client",
    "get_streaming_client",
    "close_streaming_client",
    "refresh_port_cache",
    "get_target_url",
    "aget_target_url",
    "_serialize_body",
    "_save_to_db",
    "_save_record_async",
    "_sanitize_text",
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
                print(
                    f"[Proxy] WARNING: {label} body contains surrogate "
                    f"characters after JSON serialization — sanitizing",
                    file=sys.stderr,
                )
                result = result.encode("utf-8", errors="replace").decode("utf-8")
            return result
        except (json.JSONDecodeError, Exception):
            return text
    except UnicodeDecodeError:
        print(
            f"[Proxy] WARNING: request body is not valid UTF-8 — "
            f"storing as binary placeholder ({len(body_bytes)} bytes)",
            file=sys.stderr,
        )
        return f"[binary data, {len(body_bytes)} bytes]"


# ──────────────────────────────────────────────
#  SSE format auto-detection
# ──────────────────────────────────────────────

# Anthropic Messages API event types (including error and ping for detection)
_ANTHROPIC_EVENT_TYPES = frozenset({
    "message_start", "content_block_start", "content_block_delta",
    "content_block_stop", "message_delta", "message_stop",
    "ping", "error",
})

# OpenAI Responses API event type prefixes
_OPENAI_RESPONSES_PREFIXES = (
    "response.", "error",
)


def _detect_sse_format(first_chunk: dict) -> str:
    """Detect the SSE stream format from the first parsed JSON chunk.

    Returns one of:
      'anthropic'       — Anthropic Messages API
      'openai_responses'— OpenAI Responses API  (/v1/responses)
      'gemini'          — Google Gemini API
      'openai_chat'     — OpenAI Chat Completions  (/v1/chat/completions)
      'generic'         — Unknown format, try best-effort extraction
    """
    event_type = first_chunk.get("type", "")

    # Anthropic: type is a known Anthropic event name
    if event_type in _ANTHROPIC_EVENT_TYPES:
        return "anthropic"

    # OpenAI Responses API: type starts with "response."
    if isinstance(event_type, str) and event_type.startswith(_OPENAI_RESPONSES_PREFIXES):
        return "openai_responses"

    # Google Gemini: has a "candidates" array
    if "candidates" in first_chunk:
        return "gemini"

    # OpenAI Chat Completions: has "choices" with nested "delta"
    if "choices" in first_chunk:
        return "openai_chat"

    return "generic"


# ──────────────────────────────────────────────
#  Format-specific parsers
# ──────────────────────────────────────────────

def _parse_anthropic_sse(raw_sse: str) -> str | None:
    """Parse Anthropic Messages API SSE stream → reconstructed Messages JSON.

    Handles: text, thinking (extended thinking), tool_use, and server_tool_use blocks."""

    # ── Event type constants ──
    MSG_START = "message_start"
    CB_START = "content_block_start"
    CB_DELTA = "content_block_delta"
    CB_STOP = "content_block_stop"
    MSG_DELTA = "message_delta"
    MSG_STOP = "message_stop"

    # Anthropic delta types for content_block_delta
    DELTA_TEXT = "text_delta"
    DELTA_THINKING = "thinking_delta"
    DELTA_SIGNATURE = "signature_delta"
    DELTA_INPUT_JSON = "input_json_delta"
    DELTA_CITATIONS = "citations_delta"
    DELTA_COMPACTION = "compaction_delta"

    # Anthropic content block types we handle
    BLOCK_TEXT = "text"
    BLOCK_TOOL_USE = "tool_use"
    BLOCK_SERVER_TOOL_USE = "server_tool_use"
    BLOCK_THINKING = "thinking"
    BLOCK_COMPACTION = "compaction"
    BLOCK_SEARCH_TOOL = "search_tool"

    blocks: dict[int, dict] = {}
    # Each block: {"type": str, "text": str, "thinking": str, "signature": str,
    #               "id": str, "name": str, "input_json": str}
    stop_reason = None
    stop_sequence = None
    stop_details = None
    usage_output = None
    usage_input = None
    model = ""
    msg_id = ""
    role = "assistant"

    try:
        for line in raw_sse.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue

            event = json.loads(payload)
            event_type = event.get("type", "")

            # ── message_start ──
            if event_type == MSG_START:
                message = event.get("message", {})
                msg_id = message.get("id", "")
                model = message.get("model", "")
                role = message.get("role", "assistant")
                # Input tokens usage
                if message.get("usage"):
                    usage_input = message["usage"]

            # ── content_block_start ──
            elif event_type == CB_START:
                idx = event.get("index", 0)
                cb = event.get("content_block", {})
                block_type = cb.get("type", "text")

                blocks[idx] = {"type": block_type}

                if block_type in (BLOCK_TOOL_USE, BLOCK_SERVER_TOOL_USE):
                    blocks[idx].update({
                        "id": cb.get("id", ""),
                        "name": cb.get("name", ""),
                        "input_json": "",
                    })
                elif block_type == BLOCK_THINKING:
                    # content_block_start for thinking only has {"type": "thinking"}
                    # thinking text and signature come via content_block_delta
                    blocks[idx].update({
                        "thinking": "",
                        "signature": "",
                    })
                elif block_type == BLOCK_COMPACTION:
                    # compaction content block: content and encrypted_content come via delta
                    blocks[idx].update({
                        "content": "",
                        "encrypted_content": "",
                    })
                elif block_type == BLOCK_TEXT:
                    blocks[idx]["text"] = cb.get("text", "")
                    # Citations may accumulate
                    blocks[idx]["citations"] = []
                elif block_type in (BLOCK_SEARCH_TOOL,):
                    blocks[idx].update({
                        "id": cb.get("id", ""),
                        "name": cb.get("name", ""),
                        "input_json": "",
                    })
                else:
                    # Unknown future block type — accumulate as generic text
                    blocks[idx]["_text"] = cb.get("text", "")

            # ── content_block_delta ──
            elif event_type == CB_DELTA:
                idx = event.get("index", 0)
                delta = event.get("delta", {})
                delta_type = delta.get("type", "")

                if idx not in blocks:
                    # content_block_start was missed — infer type from delta type
                    if delta_type == DELTA_INPUT_JSON:
                        blocks[idx] = {"type": BLOCK_TOOL_USE, "id": "", "name": "", "input_json": ""}
                    elif delta_type == DELTA_THINKING:
                        blocks[idx] = {"type": BLOCK_THINKING, "thinking": "", "signature": ""}
                    elif delta_type == DELTA_SIGNATURE:
                        blocks[idx] = {"type": BLOCK_THINKING, "thinking": "", "signature": ""}
                    elif delta_type == DELTA_COMPACTION:
                        blocks[idx] = {"type": BLOCK_COMPACTION, "content": "", "encrypted_content": ""}
                    else:
                        blocks[idx] = {"type": BLOCK_TEXT, "text": "", "citations": []}

                if delta_type == DELTA_TEXT:
                    blocks[idx]["text"] = blocks[idx].get("text", "") + delta.get("text", "")
                elif delta_type == DELTA_THINKING:
                    blocks[idx]["thinking"] = blocks[idx].get("thinking", "") + delta.get("thinking", "")
                elif delta_type == DELTA_SIGNATURE:
                    blocks[idx]["signature"] = blocks[idx].get("signature", "") + delta.get("signature", "")
                elif delta_type == DELTA_INPUT_JSON:
                    blocks[idx]["input_json"] = (
                        blocks[idx].get("input_json", "")
                        + delta.get("partial_json", "")
                    )
                elif delta_type == DELTA_CITATIONS:
                    # citations accumulate as a list
                    if "citations" not in blocks[idx]:
                        blocks[idx]["citations"] = []
                    if delta.get("citation"):
                        blocks[idx]["citations"].append(delta["citation"])
                elif delta_type == DELTA_COMPACTION:
                    blocks[idx]["content"] = delta.get("content", "")
                    blocks[idx]["encrypted_content"] = delta.get("encrypted_content", "")
                else:
                    # Unknown delta type — concatenate all string values as text
                    for dk, dv in delta.items():
                        if dk != "type" and isinstance(dv, str):
                            blocks[idx].setdefault("text", "")
                            blocks[idx]["text"] += dv

            # ── content_block_stop ── (no data needed, index is enough)
            elif event_type == CB_STOP:
                pass

            # ── message_delta ──
            elif event_type == MSG_DELTA:
                delta = event.get("delta", {})
                if delta.get("stop_reason"):
                    stop_reason = delta["stop_reason"]
                if delta.get("stop_sequence") is not None:
                    stop_sequence = delta["stop_sequence"]
                if delta.get("stop_details") is not None:
                    stop_details = delta["stop_details"]
                # Output tokens usage
                if event.get("usage"):
                    usage_output = event["usage"]

            # ── message_stop ── (end of stream, no data)
            elif event_type == MSG_STOP:
                pass

            # ── error ── (upstream API reported an error — capture it)
            elif event_type == "error":
                err_info = event.get("error", {})
                stop_reason = "error"
                stop_details = {"api_error": err_info}
                print(
                    f"[Proxy] Anthropic SSE stream contains error event: "
                    f"{err_info}",
                    file=sys.stderr,
                )

        # ── Build reconstructed message ──
        content_blocks = []
        for idx in sorted(blocks.keys()):
            b = blocks[idx]
            block_type = b.get("type", "text")

            if block_type == BLOCK_TEXT:
                text_block = {"type": "text", "text": b.get("text", "")}
                if b.get("citations"):
                    text_block["citations"] = b["citations"]
                content_blocks.append(text_block)

            elif block_type == BLOCK_THINKING:
                content_blocks.append({
                    "type": "thinking",
                    "thinking": b.get("thinking", ""),
                    "signature": b.get("signature", ""),
                })

            elif block_type == BLOCK_COMPACTION:
                content_blocks.append({
                    "type": "compaction",
                    "content": b.get("content", ""),
                    "encrypted_content": b.get("encrypted_content", ""),
                })

            elif block_type in (BLOCK_TOOL_USE, BLOCK_SERVER_TOOL_USE, BLOCK_SEARCH_TOOL):
                tool_block = {
                    "type": block_type,
                    "id": b.get("id", ""),
                    "name": b.get("name", ""),
                    "input": {},
                }
                raw_input = b.get("input_json", "")
                if raw_input:
                    try:
                        tool_block["input"] = json.loads(raw_input)
                    except json.JSONDecodeError:
                        tool_block["input"] = raw_input
                content_blocks.append(tool_block)

            else:
                # Unknown block type — emit as-is with any accumulated content
                plain = {"type": block_type}
                if b.get("text"):
                    plain["text"] = b["text"]
                for key in ("id", "name", "thinking", "signature"):
                    if b.get(key):
                        plain[key] = b[key]
                if b.get("_text"):
                    plain["content"] = b["_text"]
                content_blocks.append(plain)

        # Merge input + output usage (preserve all fields from both)
        usage = None
        if usage_input or usage_output:
            usage = {}
            if usage_input:
                usage.update(usage_input)
            if usage_output:
                usage.update(usage_output)

        reconstructed = {
            "id": msg_id,
            "type": "message",
            "role": role,
            "content": content_blocks,
            "model": model,
            "stop_reason": stop_reason,
            "stop_sequence": stop_sequence,
        }
        if stop_details is not None:
            reconstructed["stop_details"] = stop_details
        if usage:
            reconstructed["usage"] = usage

        return json.dumps(reconstructed, ensure_ascii=False, indent=2)
    except Exception:
        print("[Proxy] WARNING: failed to reconstruct Anthropic SSE to JSON",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None


def _parse_openai_chat_sse(raw_sse: str) -> str | None:
    """Parse OpenAI Chat Completions SSE stream → reconstructed chat.completion JSON.

    Handles: choices[0].delta.content, reasoning_content, tool_calls."""
    full_content = ""
    full_reasoning = ""
    finish_reason = None
    usage = None
    model = ""
    obj_type = ""
    msg_id = ""
    created = 0
    role = "assistant"
    # Accumulate tool_calls by index
    tool_calls: dict[int, dict] = {}

    try:
        for line in raw_sse.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue

            chunk = json.loads(payload)
            if not msg_id:
                msg_id = chunk.get("id", "")
                obj_type = chunk.get("object", "").replace(".chunk", "")
                model = chunk.get("model", "")
                created = chunk.get("created", 0)

            if chunk.get("usage"):
                usage = chunk["usage"]

            choices = chunk.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})
            if delta.get("content"):
                full_content += delta["content"]
            if delta.get("reasoning_content"):
                full_reasoning += delta["reasoning_content"]

            # Accumulate streaming tool calls
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.get("id"):
                    tool_calls[idx]["id"] = tc["id"]
                if tc.get("function", {}).get("name"):
                    tool_calls[idx]["function"]["name"] += tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    tool_calls[idx]["function"]["arguments"] += tc["function"]["arguments"]

            if choices[0].get("finish_reason") and not finish_reason:
                finish_reason = choices[0]["finish_reason"]

        message: dict = {"role": role, "content": full_content}
        if full_reasoning:
            message["reasoning_content"] = full_reasoning
        if tool_calls:
            message["tool_calls"] = [
                tool_calls[i] for i in sorted(tool_calls.keys())
            ]

        reconstructed = {
            "id": msg_id,
            "object": obj_type or "chat.completion",
            "model": model,
            "created": created,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or "stop",
            }],
        }
        if usage:
            reconstructed["usage"] = usage

        return json.dumps(reconstructed, ensure_ascii=False, indent=2)
    except Exception:
        print("[Proxy] WARNING: failed to reconstruct OpenAI Chat SSE to JSON",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None


def _parse_openai_responses_sse(raw_sse: str) -> str | None:
    """Parse OpenAI Responses API SSE stream → reconstructed response JSON.

    Key events: response.output_text.delta, response.reasoning_summary_text.delta,
    response.function_call_arguments.delta, response.created, response.completed."""
    output_text = ""
    reasoning_text = ""
    function_args = ""
    response_id = ""
    model = ""
    status = ""
    usage = None
    output_items: list[dict] = []

    try:
        for line in raw_sse.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue

            event = json.loads(payload)
            event_type = event.get("type", "")

            if event_type == "response.created":
                resp = event.get("response", {})
                response_id = resp.get("id", "")
                model = resp.get("model", "")
                status = resp.get("status", "")

            elif event_type == "response.completed":
                resp = event.get("response", {})
                if resp.get("usage"):
                    usage = resp["usage"]
                status = "completed"
                # The completed response may have the full output array
                if resp.get("output"):
                    output_items = resp["output"]

            elif event_type == "response.output_text.delta":
                output_text += event.get("delta", "")

            elif event_type == "response.reasoning_summary_text.delta":
                reasoning_text += event.get("delta", "")

            elif event_type == "response.function_call_arguments.delta":
                function_args += event.get("delta", "")

            elif event_type == "response.failed":
                status = "failed"

        # Build reconstructed response
        reconstructed: dict = {
            "id": response_id,
            "object": "response",
            "model": model,
            "status": status or "completed",
        }

        # If we captured output items from response.completed, use those
        if output_items:
            reconstructed["output"] = output_items
        else:
            # Build output from accumulated deltas
            output = []
            if output_text:
                output.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": output_text}],
                })
            if reasoning_text:
                output.append({
                    "type": "reasoning",
                    "summary": [{"type": "reasoning_summary_text", "text": reasoning_text}],
                })
            if function_args:
                try:
                    parsed_args = json.loads(function_args)
                except json.JSONDecodeError:
                    parsed_args = function_args
                output.append({
                    "type": "function_call",
                    "arguments": parsed_args,
                })
            reconstructed["output"] = output

        if usage:
            reconstructed["usage"] = usage

        return json.dumps(reconstructed, ensure_ascii=False, indent=2)
    except Exception:
        print("[Proxy] WARNING: failed to reconstruct OpenAI Responses SSE to JSON",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None


def _parse_gemini_sse(raw_sse: str) -> str | None:
    """Parse Google Gemini SSE stream → reconstructed generateContent JSON.

    Each SSE chunk is a full GenerateContentResponse. We collect the text from
    candidates[0].content.parts[*].text across all chunks (Gemini sends
    incremental text in each chunk)."""
    full_text = ""
    finish_reason = None
    usage = None
    model = ""
    response_id = ""
    role = "model"

    try:
        for line in raw_sse.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue

            chunk = json.loads(payload)

            # Metadata
            if not model:
                model = chunk.get("modelVersion", "")
            if not response_id:
                response_id = chunk.get("responseId", "")

            if chunk.get("usageMetadata"):
                usage = chunk["usageMetadata"]

            candidates = chunk.get("candidates", [])
            if not candidates:
                continue

            candidate = candidates[0]
            content = candidate.get("content", {})
            role = content.get("role", "model")
            parts = content.get("parts", [])

            for part in parts:
                if "text" in part:
                    full_text += part["text"]

            if candidate.get("finishReason") and not finish_reason:
                finish_reason = candidate["finishReason"]

        reconstructed = {
            "responseId": response_id,
            "modelVersion": model,
            "candidates": [{
                "index": 0,
                "content": {
                    "role": role,
                    "parts": [{"text": full_text}],
                },
                "finishReason": finish_reason or "STOP",
            }],
        }
        if usage:
            reconstructed["usageMetadata"] = usage

        return json.dumps(reconstructed, ensure_ascii=False, indent=2)
    except Exception:
        print("[Proxy] WARNING: failed to reconstruct Gemini SSE to JSON",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None


def _deep_merge(base: dict, chunk: dict) -> dict:
    """Deep merge two dicts. Strings concatenate, lists merge by index, dicts recurse.
    
    This is the universal SSE reconstruction algorithm:
    - All SSE chunks share the same top-level structure
    - Incremental content is in string fields that should be concatenated
    - Metadata fields (id, model, etc.) appear in early chunks and are preserved
    """
    result = dict(base)
    for key, new_val in chunk.items():
        if key not in result:
            result[key] = new_val
        else:
            old_val = result[key]
            if isinstance(old_val, str) and isinstance(new_val, str):
                # Strings concatenate (this is the key for SSE!)
                result[key] = old_val + new_val
            elif isinstance(old_val, dict) and isinstance(new_val, dict):
                result[key] = _deep_merge(old_val, new_val)
            elif isinstance(old_val, list) and isinstance(new_val, list):
                result[key] = _merge_lists(old_val, new_val)
            else:
                # Overwrite with new value (for numbers, booleans, null)
                result[key] = new_val
    return result


def _merge_lists(base_list: list, new_list: list) -> list:
    """Merge lists by index. If new_list has items at same indices, deep merge them."""
    result = list(base_list)
    for i, item in enumerate(new_list):
        if i < len(result):
            if isinstance(result[i], dict) and isinstance(item, dict):
                result[i] = _deep_merge(result[i], item)
            elif isinstance(result[i], str) and isinstance(item, str):
                result[i] = result[i] + item
            else:
                result[i] = item
        else:
            result.append(item)
    return result


def _reconstruct_sse_universal(raw_sse: str) -> str | None:
    """Universal SSE reconstruction using deep merge.
    
    Works for any format where all chunks share the same structure:
    - OpenAI Chat Completions
    - Google Gemini
    - Any OpenAI-compatible API (DeepSeek, Mistral, Together, etc.)
    
    For event-based formats (Anthropic, OpenAI Responses), use format-specific parsers.
    """
    if not raw_sse:
        return None
    
    merged = {}
    try:
        for line in raw_sse.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            
            chunk = json.loads(payload)
            merged = _deep_merge(merged, chunk)
        
        # Post-processing for OpenAI-like formats
        # Rename delta → message in choices (OpenAI Chat Completions pattern)
        for choice in merged.get("choices", []):
            if "delta" in choice:
                choice["message"] = choice.pop("delta")
        
        return json.dumps(merged, ensure_ascii=False, indent=2)
    except Exception:
        print("[Proxy] WARNING: universal SSE reconstruction failed", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None


def _parse_generic_sse(raw_sse: str) -> str | None:
    """Best-effort SSE parser for unknown formats.
    
    Strategy:
    1. Try universal deep merge (works for OpenAI-like formats)
    2. If that fails or produces empty content, fall back to text extraction
    3. If nothing works, return raw SSE text
    """
    if not raw_sse:
        return None

    # Try universal deep merge first
    result = _reconstruct_sse_universal(raw_sse)
    if result and result != "{}":
        # Check if the merged result has meaningful content
        try:
            parsed = json.loads(result)
            # If it has choices with message content, or candidates with text, it's good
            if parsed.get("choices") or parsed.get("candidates"):
                return result
        except json.JSONDecodeError:
            pass
    
    # Fallback: extract text from common paths
    all_text = ""
    try:
        for line in raw_sse.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue

            obj = json.loads(payload)

            # Try OpenAI-compatible
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or choice.get("message") or {}
                if delta.get("content"):
                    all_text += delta["content"]
                    continue
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    if fn.get("arguments"):
                        all_text += fn["arguments"]

            # Try Gemini-compatible
            for cand in obj.get("candidates") or []:
                for part in (cand.get("content") or {}).get("parts") or []:
                    if part.get("text"):
                        all_text += part["text"]

            # Try Anthropic-like
            delta = obj.get("delta", {})
            if delta.get("text"):
                all_text += delta["text"]
            if delta.get("partial_json"):
                all_text += delta["partial_json"]

            # Recursive walk for "content" / "text" / "delta" leaves
            if not all_text:
                all_text += _walk_json_for_text(obj)

        # If we found text, return a simple reconstructed object
        if all_text.strip():
            reconstructed = {
                "_format": "generic",
                "content": all_text,
                "note": "Best-effort reconstruction — see response_body_raw for original SSE",
            }
            return json.dumps(reconstructed, ensure_ascii=False, indent=2)

        # Nothing found — return raw SSE so it's at least visible
        return raw_sse
    except Exception:
        print("[Proxy] WARNING: failed to reconstruct generic SSE to JSON",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return raw_sse


def _walk_json_for_text(obj, depth: int = 0) -> str:
    """Recursively walk a JSON object looking for string values at
    common text-bearing keys. Depth-limited to prevent runaway."""
    if depth > 10:
        return ""
    if isinstance(obj, str):
        return ""
    if isinstance(obj, dict):
        pieces = []
        for key in ("content", "text", "delta", "value", "data",
                     "reasoning_content", "reasoning"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                pieces.append(val)
        # Recurse into nested objects/arrays
        for val in obj.values():
            pieces.append(_walk_json_for_text(val, depth + 1))
        return "".join(pieces)
    if isinstance(obj, list):
        return "".join(_walk_json_for_text(item, depth + 1) for item in obj)
    return ""


# ──────────────────────────────────────────────
#  Dispatcher
# ──────────────────────────────────────────────

def _reconstruct_sse_to_json(raw_sse: str) -> str | None:
    """Parse SSE (text/event-stream) raw text and reconstruct a full
    non-streaming response JSON by merging all delta chunks.

    Supports these formats (auto-detected):
      • Anthropic Messages API
      • OpenAI Chat Completions API
      • OpenAI Responses API
      • Google Gemini API
      • Generic / unknown (best-effort extraction)

    Returns the reconstructed JSON string, or None if parsing fails.
    """
    if not raw_sse:
        return None

    # Detect format from the first valid data line
    for line in raw_sse.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            first_chunk = json.loads(payload)
            fmt = _detect_sse_format(first_chunk)

            if fmt == "anthropic":
                result = _parse_anthropic_sse(raw_sse)
                if result is not None:
                    return result
                print(
                    "[Proxy] Anthropic parser returned None, "
                    "falling back to universal",
                    file=sys.stderr,
                )
            elif fmt == "openai_responses":
                result = _parse_openai_responses_sse(raw_sse)
                if result is not None:
                    return result
                print(
                    "[Proxy] OpenAI Responses parser returned None, "
                    "falling back to universal",
                    file=sys.stderr,
                )
            elif fmt == "gemini":
                result = _parse_gemini_sse(raw_sse)
                if result is not None:
                    return result
            elif fmt == "openai_chat":
                result = _parse_openai_chat_sse(raw_sse)
                if result is not None:
                    return result
            # Always try universal as ultimate fallback
            return _parse_generic_sse(raw_sse)
        except (json.JSONDecodeError, Exception):
            continue

    return None



# NOTE: The old per-port proxy endpoint (proxy_endpoint / create_proxy_app)
# was removed in favour of the shared-proxy architecture in shared_proxy.py.
# All proxy traffic now flows through shared_proxy_endpoint().
