"""
Shared Proxy — a single-endpoint reverse proxy that routes requests
based on the port number embedded in the URL path.

Users only need to change their base_url:
  Original:  https://api.openai.com/v1/chat/completions
  Proxy:     http://server:3998/4001/v1/chat/completions
                        ↑ port_number in path

No API keys, no header modifications needed.
The port_number identifies the user's proxy configuration.
"""
from __future__ import annotations
import time
import json
import sys
import asyncio
import tempfile
import httpx
from urllib.parse import urlparse
from fastapi import APIRouter, Request
from fastapi.responses import Response, JSONResponse, StreamingResponse

from proxy_app import (
    get_shared_client,
    _reconstruct_sse_to_json,
    aget_target_url,
    _serialize_body,
    _save_record_async,
    EXCLUDE_HEADERS,
)
from config import PROXY_BODY_MEMORY_LIMIT

router = APIRouter()


@router.api_route("/{port_number}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def shared_proxy_endpoint(request: Request, port_number: int, path: str):
    """Shared proxy handler — routes by port_number in URL path.

    All users share this single endpoint. The port_number in the path
    identifies which proxy configuration (target_url) to use.

    Example:
      POST http://server:3998/4001/v1/chat/completions
      → forwards to the target_url configured for port 4001
    """
    start_time = time.time()

    # Look up target URL for this port_number (async — DB query in thread pool)
    target_url = await aget_target_url(port_number)
    if not target_url:
        return JSONResponse(
            {"error": f"No active proxy configured for port {port_number}"},
            status_code=404,
        )

    # Build the actual forward path (strip the /{port_number} prefix)
    forward_path = "/" + path if path else "/"
    query_string = str(request.url.query)

    target_full_url = f"{target_url.rstrip('/')}{forward_path}"
    if query_string:
        target_full_url += f"?{query_string}"

    print(f"[Proxy] {request.method} #{port_number} → {target_full_url}", file=sys.stderr)

    # ── Read request body with spill-to-disk ──
    # SpooledTemporaryFile keeps data in memory up to PROXY_BODY_MEMORY_LIMIT,
    # then transparently spills to disk.  Avoids OOM on huge uploads while
    # keeping zero-disk-IO performance for typical LLM API requests.
    req_buf = tempfile.SpooledTemporaryFile(max_size=PROXY_BODY_MEMORY_LIMIT)
    try:
        async for chunk in request.stream():
            req_buf.write(chunk)
    except Exception as e:
        print(f"[Proxy] WARNING: Failed to read request body: {e}", file=sys.stderr)
        # empty body or read error — forward whatever we have
    req_buf.seek(0)
    body = req_buf.read()
    req_buf.close()

    # Serialize request for logging
    req_headers_dict = dict(request.headers)
    req_headers_json = json.dumps(req_headers_dict, ensure_ascii=False, indent=2)
    req_body_str = _serialize_body(body, label="request")

    # Prepare forward headers (same logic as per-port proxy)
    forward_headers = {
        k: v for k, v in req_headers_dict.items()
        if k.lower() not in EXCLUDE_HEADERS
    }
    parsed_target = urlparse(target_url)
    forward_headers["host"] = parsed_target.netloc
    forward_headers.pop("accept-encoding", None)

    # Forward the request
    status_code = 502
    resp_body_str = None
    resp_body_raw_str = None
    resp_headers_json = "{}"
    resp_content_type = "application/json"
    is_streaming = False
    stream_owned_by_generator = False
    stream_ctx = None

    try:
        client = get_shared_client()

        stream_ctx = client.stream(
            method=request.method,
            url=target_full_url,
            headers=forward_headers,
            content=body if body else None,
        )
        response = await stream_ctx.__aenter__()

        status_code = response.status_code
        resp_headers_json = json.dumps(dict(response.headers), ensure_ascii=False, indent=2)
        resp_content_type = response.headers.get("content-type", "application/json")

        is_streaming = "text/event-stream" in resp_content_type

        if is_streaming:
            # --- STREAMING: SpooledTemporaryFile per stream ---
            # Each chunk is yielded to the client immediately *and* buffered
            # in a SpooledTemporaryFile (memory up to PROXY_BODY_MEMORY_LIMIT,
            # then spills to disk).  After the stream ends the full body is
            # reconstructed → saved to requests in a single fire-and-forget write.
            # No MySQL write-ahead, no background worker, no zombie cleanup.
            stream_owned_by_generator = True

            async def stream_generator():
                resp_buf = tempfile.SpooledTemporaryFile(
                    max_size=PROXY_BODY_MEMORY_LIMIT,
                )
                try:
                    async for chunk in response.aiter_bytes():
                        yield chunk           # ← client gets it immediately
                        resp_buf.write(chunk)  # buffer (memory or disk)

                    # Stream ended normally — reconstruct JSON and save
                    resp_buf.seek(0)
                    full_body = resp_buf.read()
                    raw_sse_text = full_body.decode("utf-8", errors="replace")
                    reconstructed_json = _reconstruct_sse_to_json(raw_sse_text)

                    # Detect reconstruction failure: None means all parsers exhausted;
                    # raw SSE text (starts with "data:") means generic fallback returned.
                    reconstruction_error = (
                        reconstructed_json is None
                        or reconstructed_json.lstrip().startswith("data:")
                    )
                    if reconstruction_error and reconstructed_json is None:
                        reconstructed_json = raw_sse_text

                    duration_ms = int((time.time() - start_time) * 1000)
                    asyncio.create_task(_save_record_async(
                        port_number, request.method,
                        forward_path
                        + ("?" + query_string if query_string else ""),
                        req_headers_json, req_body_str,
                        resp_headers_json, reconstructed_json,
                        status_code, duration_ms,
                        resp_body_raw=raw_sse_text,
                        reconstruction_error=reconstruction_error,
                    ))
                finally:
                    resp_buf.close()
                    await stream_ctx.__aexit__(None, None, None)

            response_headers = {"X-Proxy-Port": str(port_number)}
            return StreamingResponse(
                stream_generator(),
                status_code=status_code,
                media_type=resp_content_type,
                headers=response_headers,
            )
        else:
            # --- NON-STREAMING: read full response with spill-to-disk, save, return ---
            resp_buf = tempfile.SpooledTemporaryFile(max_size=PROXY_BODY_MEMORY_LIMIT)
            try:
                async for chunk in response.aiter_bytes():
                    resp_buf.write(chunk)
                resp_buf.seek(0)
                full_body = resp_buf.read()
            finally:
                resp_buf.close()
            await stream_ctx.__aexit__(None, None, None)
            stream_owned_by_generator = False
            resp_body_str = _serialize_body(full_body, label="response")
            try:
                resp_body_raw_str = full_body.decode("utf-8", errors="replace")
            except Exception:
                resp_body_raw_str = resp_body_str

    except httpx.TimeoutException as e:
        status_code = 504
        resp_body_str = json.dumps({"error": f"Upstream timeout: {type(e).__name__}"})
        print(f"[Proxy] Timeout → {target_full_url}: {type(e).__name__}: {e}", file=sys.stderr)
        is_streaming = False
    except httpx.ConnectError as e:
        status_code = 502
        # Dig into the exception chain to get the real error
        inner = e.__cause__ or e.__context__ or None
        if inner is None:
            inner_str = "no further detail"
        else:
            # Some httpcore exceptions nest their cause
            inner2 = inner.__cause__ or inner.__context__ or None
            inner_str = f"{type(inner).__name__}: {inner}" if inner2 is None else f"{type(inner2).__name__}: {inner2}"
        print(f"[Proxy] ConnectError → {target_full_url}: {type(e).__name__}: {e} | inner: {inner_str}", file=sys.stderr)
        resp_body_str = json.dumps({"error": f"Cannot connect to upstream: {inner_str}"})
        is_streaming = False
    except Exception as e:
        status_code = 502
        detail = str(e) or repr(e)
        print(f"[Proxy] Error → {target_full_url}: {type(e).__name__}: {detail}", file=sys.stderr)
        resp_body_str = json.dumps({"error": f"Proxy error: {detail}"})
        is_streaming = False
    finally:
        if stream_ctx is not None and not stream_owned_by_generator:
            try:
                await stream_ctx.__aexit__(None, None, None)
            except Exception as e:
                print(f"[Proxy] WARNING: Failed to close stream context: {e}", file=sys.stderr)

    duration_ms = int((time.time() - start_time) * 1000)

    # Save non-streaming record in background
    if not is_streaming:
        # For non-streaming responses, resp_body_str holds the pretty-printed
        # JSON; resp_body_raw_str holds the raw decoded response body so
        # response_body_raw is populated consistently with the streaming path.
        asyncio.create_task(_save_record_async(
            port_number, request.method,
            forward_path + ("?" + query_string if query_string else ""),
            req_headers_json, req_body_str,
            resp_headers_json, resp_body_str,
            status_code, duration_ms,
            resp_body_raw=resp_body_raw_str or resp_body_str,
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
