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
from urllib.parse import urlparse
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, StreamingResponse
import database
from models import Port, Request as RequestModel


# Shared httpx client with connection pooling — reused across all proxy requests.
# Limits: 100 total connections, 20 per host. Handles 100+ concurrent users.
_shared_client: httpx.AsyncClient | None = None


def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=15.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            follow_redirects=False,
            verify=certifi.where(),
        )
    return _shared_client


async def close_shared_client():
    global _shared_client
    if _shared_client:
        await _shared_client.aclose()
        _shared_client = None


# In-memory cache of port -> target_url mappings for fast lookup
_port_target_cache: dict[int, str] = {}


def refresh_port_cache(db=None):
    """Refresh the port -> target_url cache from database."""
    global _port_target_cache
    if db is None:
        db = database.SessionLocal()
        try:
            ports = db.query(Port).filter(Port.is_active.is_(True)).all()
            _port_target_cache = {p.port_number: p.target_url for p in ports}
        finally:
            db.close()
    else:
        ports = db.query(Port).filter(Port.is_active.is_(True)).all()
        _port_target_cache = {p.port_number: p.target_url for p in ports}


def get_target_url(port_number: int) -> str | None:
    """Get target URL for a port. Falls back to DB if not in cache."""
    if port_number in _port_target_cache:
        return _port_target_cache[port_number]

    db = database.SessionLocal()
    try:
        port = db.query(Port).filter(
            Port.port_number == port_number,
            Port.is_active.is_(True)
        ).first()
        if port:
            _port_target_cache[port.port_number] = port.target_url
            return port.target_url
        return None
    finally:
        db.close()


def _save_to_db(port_number: int, method: str, path: str,
                req_headers: str, req_body: str | None,
                resp_headers: str, resp_body: str | None,
                status_code: int, duration_ms: int,
                resp_body_raw: str | None = None):
    """Save a request/response record to the database. Runs in a thread.
    Retries up to 3 times on transient connection errors."""
    import time as _time
    last_error = None
    for attempt in range(3):
        db = database.SessionLocal()
        try:
            port = db.query(Port).filter(
                Port.port_number == port_number,
                Port.is_active.is_(True)
            ).first()
            if not port:
                print(f"[Proxy] WARNING: port {port_number} not found in DB, cannot save record",
                      file=sys.stderr)
                return

            record = RequestModel(
                port_id=port.id,
                method=method,
                path=path,
                request_headers=req_headers,
                request_body=req_body,
                response_headers=resp_headers,
                response_body=resp_body,
                response_body_raw=resp_body_raw,
                status_code=status_code,
                duration_ms=duration_ms,
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
                              resp_body_raw: str | None = None):
    """Async wrapper — runs the sync DB save in a thread to avoid blocking."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        _save_to_db,
        port_number, method, path,
        req_headers, req_body,
        resp_headers, resp_body,
        status_code, duration_ms,
        resp_body_raw,
    )


# Headers to exclude when forwarding
EXCLUDE_HEADERS = {
    "host", "content-length", "connection", "transfer-encoding",
    "content-encoding",  # httpx decompresses for us
}


def _serialize_body(body_bytes: bytes) -> str | None:
    """Convert raw body bytes to a pretty-printed string."""
    if not body_bytes:
        return None
    try:
        text = body_bytes.decode("utf-8")
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, Exception):
            return text
    except UnicodeDecodeError:
        return f"[binary data, {len(body_bytes)} bytes]"


def _reconstruct_sse_to_json(raw_sse: str) -> str | None:
    """Parse SSE (text/event-stream) raw text and reconstruct a full
    non-streaming chat completion JSON by merging all delta chunks.

    Returns the reconstructed JSON string, or None if parsing fails.
    """
    if not raw_sse:
        return None

    full_content = ""
    full_reasoning = ""
    finish_reason = None
    usage = None
    model = ""
    obj_type = ""
    msg_id = ""
    created = 0
    role = "assistant"

    try:
        for line in raw_sse.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()  # remove "data:" prefix
            if not payload or payload == "[DONE]":
                continue

            chunk = json.loads(payload)
            # Collect metadata from first chunk
            if not msg_id:
                msg_id = chunk.get("id", "")
                obj_type = chunk.get("object", "").replace(".chunk", "")
                model = chunk.get("model", "")
                created = chunk.get("created", 0)

            # Collect usage from the chunk that has it
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
            if choices[0].get("finish_reason") and not finish_reason:
                finish_reason = choices[0]["finish_reason"]

        # Build reconstructed full response
        message = {"role": role, "content": full_content}
        if full_reasoning:
            message["reasoning_content"] = full_reasoning

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
        print("[Proxy] WARNING: failed to reconstruct SSE to JSON", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None


async def proxy_endpoint(request: Request):
    """Main proxy handler — intercepts, forwards, records.
    Handles both regular JSON and streaming (SSE) responses."""
    start_time = time.time()

    # Determine the port this request came in on
    port_number = request.scope.get("server", (None, None))[1]
    if not port_number:
        return JSONResponse({"error": "Cannot determine port"}, status_code=500)

    # Look up target URL
    target_url = get_target_url(port_number)
    if not target_url:
        return JSONResponse(
            {"error": f"No active proxy configured for port {port_number}"},
            status_code=404
        )

    # Build target URL
    path = request.url.path
    query_string = str(request.url.query)
    target_full_url = f"{target_url.rstrip('/')}{path}"
    if query_string:
        target_full_url += f"?{query_string}"

    # Read request body
    try:
        body = await request.body()
    except Exception:
        body = b""

    # Serialize request
    req_headers_dict = dict(request.headers)
    req_headers_json = json.dumps(req_headers_dict, ensure_ascii=False, indent=2)
    req_body_str = _serialize_body(body)

    # Prepare forward headers
    forward_headers = {
        k: v for k, v in req_headers_dict.items()
        if k.lower() not in EXCLUDE_HEADERS
    }
    parsed_target = urlparse(target_url)
    forward_headers["host"] = parsed_target.netloc
    # Let httpx set accept-encoding properly
    forward_headers.pop("accept-encoding", None)

    # Forward the request
    status_code = 502
    resp_body_str = None
    resp_headers_json = "{}"
    resp_content_type = "application/json"
    is_streaming = False

    try:
        client = get_shared_client()
        resp = await client.request(
            method=request.method,
            url=target_full_url,
            headers=forward_headers,
            content=body if body else None,
        )
        status_code = resp.status_code
        resp_headers_json = json.dumps(dict(resp.headers), ensure_ascii=False, indent=2)
        resp_content_type = resp.headers.get("content-type", "application/json")

        # Check if the response is streaming (SSE)
        is_streaming = "text/event-stream" in resp_content_type

        if is_streaming:
            # --- STREAMING: forward chunks in real-time, accumulate for recording ---
            chunks = []
            async def stream_generator():
                nonlocal chunks
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    yield chunk
                # After streaming completes, reconstruct full JSON and save asynchronously
                full_body = b"".join(chunks)
                raw_sse_text = full_body.decode("utf-8", errors="replace")
                reconstructed_json = _reconstruct_sse_to_json(raw_sse_text)

                await _save_record_async(
                    port_number, request.method,
                    path + ("?" + query_string if query_string else ""),
                    req_headers_json, req_body_str,
                    resp_headers_json,
                    reconstructed_json,           # response_body
                    status_code,
                    int((time.time() - start_time) * 1000),
                    resp_body_raw=raw_sse_text,   # response_body_raw
                )

            response_headers = {"X-Proxy-Port": str(port_number)}
            return StreamingResponse(
                stream_generator(),
                status_code=status_code,
                media_type=resp_content_type,
                headers=response_headers,
            )
        else:
            # --- NON-STREAMING: read full response, save, return ---
            resp_bytes = resp.content
            resp_body_str = _serialize_body(resp_bytes)

    except httpx.TimeoutException:
        status_code = 504
        resp_body_str = '{"error": "Upstream timeout"}'
    except httpx.ConnectError:
        status_code = 502
        resp_body_str = '{"error": "Cannot connect to upstream server"}'
    except Exception as e:
        status_code = 502
        resp_body_str = json.dumps({"error": f"Proxy error: {str(e)}"})

    duration_ms = int((time.time() - start_time) * 1000)

    # Save non-streaming record in background — don't block the response
    if not is_streaming:
        asyncio.create_task(_save_record_async(
            port_number, request.method,
            path + ("?" + query_string if query_string else ""),
            req_headers_json, req_body_str,
            resp_headers_json, resp_body_str,
            status_code, duration_ms,
        ))

    # Return response with original content-type
    response_headers = {"X-Proxy-Port": str(port_number)}
    if resp_body_str and status_code != 204:
        return Response(
            content=resp_body_str,
            status_code=status_code,
            media_type=resp_content_type,
            headers=response_headers,
        )
    return Response(status_code=status_code, headers=response_headers)


def create_proxy_app() -> Starlette:
    """Create a Starlette app that acts as a reverse proxy."""
    routes = [
        Route("/{path:path}", endpoint=proxy_endpoint,
              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]),
    ]
    return Starlette(routes=routes)
