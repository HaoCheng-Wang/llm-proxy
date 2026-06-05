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


# ──────────────────────────────────────────────
#  SSE format auto-detection
# ──────────────────────────────────────────────

# Anthropic Messages API event types
_ANTHROPIC_EVENT_TYPES = frozenset({
    "message_start", "content_block_start", "content_block_delta",
    "content_block_stop", "message_delta", "message_stop", "ping",
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

            elif block_type in (BLOCK_TOOL_USE, BLOCK_SERVER_TOOL_USE):
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
                return _parse_anthropic_sse(raw_sse)
            elif fmt == "openai_responses":
                return _parse_openai_responses_sse(raw_sse)
            elif fmt == "gemini":
                return _parse_gemini_sse(raw_sse)
            elif fmt == "openai_chat":
                return _parse_openai_chat_sse(raw_sse)
            else:
                return _parse_generic_sse(raw_sse)
        except (json.JSONDecodeError, Exception):
            continue

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
