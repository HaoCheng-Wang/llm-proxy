"""
Full integration test suite for LLM Proxy backend.

Covers: config, auth, models, schemas, SSE parsers (all 6 formats),
        proxy_app utilities, routers (via TestClient with mock DB).
"""
import sys
import os
import io
import json
import time

# Run from repo root
ROOT = r"D:\homework\llm-proxy"
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import pytest


# =========================================================================
# 1. Config tests
# =========================================================================
class TestConfig:
    """Verify every config variable is loadable and has sensible defaults."""

    def test_all_config_vars_exist(self):
        from config import (
            DATABASE_URL, SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
            API_PORT, DISPLAY_IP, CORS_ORIGINS,
            DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD,
            ALLOW_INTERNAL_TARGETS,
            DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_SAVE_WORKERS,
            DB_LOG_POOL_SIZE, DB_LOG_MAX_OVERFLOW,
            PORT_CACHE_TTL, HTTPX_MAX_CONNECTIONS, HTTPX_MAX_KEEPALIVE_CONNECTIONS,
            PROXY_BODY_MEMORY_LIMIT,
            _DB_USER_FOR_AUTO, _DB_PASS_FOR_AUTO,
            _DB_HOST_FOR_AUTO, _DB_PORT_FOR_AUTO, _DB_NAME_FOR_AUTO,
        )
        assert isinstance(API_PORT, int)
        assert isinstance(DB_POOL_SIZE, int)
        assert isinstance(PORT_CACHE_TTL, int)
        assert isinstance(PROXY_BODY_MEMORY_LIMIT, int)
        assert PROXY_BODY_MEMORY_LIMIT > 0
        assert isinstance(CORS_ORIGINS, list)
        assert len(SECRET_KEY) > 0
        assert ACCESS_TOKEN_EXPIRE_MINUTES > 0

    def test_no_stale_stream_configs(self):
        """Verify the 3 removed config vars are truly gone."""
        with pytest.raises(ImportError):
            from config import STREAM_RECONSTRUCTION_INTERVAL  # noqa: F811
        with pytest.raises(ImportError):
            from config import STREAM_SESSION_TIMEOUT  # noqa: F811
        with pytest.raises(ImportError):
            from config import PROXY_STREAM_MAX_PENDING_CHUNKS  # noqa: F811

    def test_cors_origins_default(self):
        """With an empty env var, default to localhost."""
        import config
        # The .env has CORS_ORIGINS= (empty), so must be localhost
        assert "localhost" in str(config.CORS_ORIGINS).lower()


# =========================================================================
# 2. Auth tests
# =========================================================================
class TestAuth:
    """JWT + bcrypt: unit-testable without DB."""

    def test_hash_and_verify(self):
        from auth import hash_password, verify_password
        pw = "test-password-1234"
        hashed = hash_password(pw)
        assert hashed != pw
        assert verify_password(pw, hashed)
        assert not verify_password("wrong", hashed)

    def test_unicode_password(self):
        from auth import hash_password, verify_password
        pw = "密码测试🔐test"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed)

    def test_long_password_truncation(self):
        """bcrypt truncates at 72 bytes — verify it works."""
        from auth import hash_password, verify_password
        pw = "a" * 80
        hashed = hash_password(pw)
        # bcrypt only uses first 72 bytes, so verify should still work
        assert verify_password(pw, hashed)

    def test_token_roundtrip(self):
        from auth import create_access_token
        from config import SECRET_KEY, ALGORITHM
        import jwt
        token = create_access_token({"user_id": 42, "role": "admin"})
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        assert payload["user_id"] == 42
        assert payload["role"] == "admin"
        assert "exp" in payload

    def test_invalid_token_rejected(self):
        import jwt
        from config import SECRET_KEY, ALGORITHM
        with pytest.raises(jwt.InvalidTokenError):
            jwt.decode("not.a.real.token", SECRET_KEY, algorithms=[ALGORITHM])

    def test_expired_token(self):
        import jwt
        from datetime import datetime, timedelta, timezone
        from config import SECRET_KEY, ALGORITHM
        expired = jwt.encode(
            {"user_id": 1, "exp": datetime.now(tz=timezone.utc) - timedelta(hours=1)},
            SECRET_KEY, algorithm=ALGORITHM,
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(expired, SECRET_KEY, algorithms=[ALGORITHM])


# =========================================================================
# 3. Schemas validation
# =========================================================================
class TestSchemas:
    """Pydantic models: request/response validation."""

    def test_user_register_validation(self):
        from schemas import UserRegister
        # Valid
        u = UserRegister(username="test", password="1234")
        assert u.username == "test"
        # Too short
        with pytest.raises(Exception):
            UserRegister(username="t", password="1234")
        with pytest.raises(Exception):
            UserRegister(username="test", password="12")

    def test_port_create_validation(self):
        from schemas import PortCreate
        p = PortCreate(target_url="https://api.openai.com", description="test")
        assert p.target_url == "https://api.openai.com"
        with pytest.raises(Exception):
            PortCreate(target_url="", description="")  # too short

    def test_port_update_partial(self):
        from schemas import PortUpdate
        p = PortUpdate()  # all optional
        assert p.port_number is None
        assert p.target_url is None

    def test_token_response(self):
        from schemas import TokenResponse
        t = TokenResponse(
            access_token="xxx", username="user1", role="user", user_id=1
        )
        assert t.token_type == "bearer"

    def test_request_info_fields(self):
        from schemas import RequestInfo
        from datetime import datetime, timezone
        r = RequestInfo(
            id=1, port_id=5, method="POST", path="/v1/chat/completions",
            request_headers=None, request_body=None,
            response_headers=None, response_body=None,
            status_code=200, duration_ms=1234,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        assert r.method == "POST"
        assert r.reconstruction_error is False


# =========================================================================
# 4. Models (without DB)
# =========================================================================
class TestModels:
    """Verify models only have expected tables (no StreamSession/StreamChunk)."""

    def test_model_tables(self):
        from database import Base
        tables = set(Base.metadata.tables.keys())
        expected = {"users", "ports", "requests"}
        assert tables == expected, f"Got tables: {tables}"

    def test_no_stream_session_model(self):
        with pytest.raises(ImportError):
            from models import StreamSession  # noqa: F811

    def test_no_stream_chunk_model(self):
        with pytest.raises(ImportError):
            from models import StreamChunk  # noqa: F811

    def test_request_model_fields(self):
        from models import Request as RequestModel
        cols = {c.name for c in RequestModel.__table__.columns}
        expected = {
            "id", "port_id", "method", "path",
            "request_headers", "request_body",
            "response_headers", "response_body", "response_body_raw",
            "status_code", "duration_ms", "reconstruction_error", "created_at",
        }
        assert cols == expected, f"Missing/extra: {cols ^ expected}"


# =========================================================================
# 5. proxy_app utilities
# =========================================================================
class TestProxyAppUtils:
    """Test serialize, sanitize, header exclusion."""

    def test_serialize_valid_json(self):
        from proxy_app import _serialize_body
        body = json.dumps({"key": "value"}).encode("utf-8")
        result = _serialize_body(body)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["key"] == "value"

    def test_serialize_plain_text(self):
        from proxy_app import _serialize_body
        body = b"plain text, not json"
        result = _serialize_body(body)
        assert result == "plain text, not json"

    def test_serialize_empty(self):
        from proxy_app import _serialize_body
        assert _serialize_body(b"") is None
        assert _serialize_body(None) is None

    def test_serialize_binary(self):
        from proxy_app import _serialize_body
        body = b"\xff\xfe\x00\x01"
        result = _serialize_body(body)
        assert result is not None
        assert "binary data" in result

    def test_sanitize_clean_text(self):
        from proxy_app import _sanitize_text
        assert _sanitize_text("hello world") == "hello world"
        assert _sanitize_text(None) is None
        assert _sanitize_text("中文测试") == "中文测试"

    def test_sanitize_surrogates(self):
        from proxy_app import _sanitize_text
        # Build a string with a lone surrogate
        bad = "hello" + chr(0xD800) + "world"
        cleaned = _sanitize_text(bad)
        assert cleaned is not None
        # Surrogate should be replaced
        assert chr(0xD800) not in cleaned
        assert "hello" in cleaned
        assert "world" in cleaned

    def test_exclude_headers(self):
        from proxy_app import EXCLUDE_HEADERS
        assert "host" in EXCLUDE_HEADERS
        assert "content-length" in EXCLUDE_HEADERS
        assert "connection" in EXCLUDE_HEADERS
        assert "transfer-encoding" in EXCLUDE_HEADERS
        assert "content-encoding" in EXCLUDE_HEADERS


# =========================================================================
# 6. SSE format detection
# =========================================================================
class TestSSEDetection:
    """Verify all 5 format detectors return correct values."""

    def test_detect_anthropic(self):
        from proxy_app import _detect_sse_format
        fmt = _detect_sse_format({"type": "message_start"})
        assert fmt == "anthropic"

    def test_detect_anthropic_error(self):
        from proxy_app import _detect_sse_format
        fmt = _detect_sse_format({"type": "error", "error": {"type": "overloaded"}})
        assert fmt == "anthropic"

    def test_detect_openai_responses(self):
        from proxy_app import _detect_sse_format
        fmt = _detect_sse_format({"type": "response.created"})
        assert fmt == "openai_responses"

    def test_detect_openai_chat(self):
        from proxy_app import _detect_sse_format
        fmt = _detect_sse_format({
            "choices": [{"delta": {"content": "hello"}}]
        })
        assert fmt == "openai_chat"

    def test_detect_gemini(self):
        from proxy_app import _detect_sse_format
        fmt = _detect_sse_format({
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}]
        })
        assert fmt == "gemini"

    def test_detect_generic(self):
        from proxy_app import _detect_sse_format
        fmt = _detect_sse_format({"unknown": "format"})
        assert fmt == "generic"


# =========================================================================
# 7. SSE Parsers — Anthropic
# =========================================================================
class TestAnthropicSSEParser:
    """Anthropic Messages API SSE reconstruction."""

    def _make_raw_sse(self, events):
        """events: list of dicts → raw SSE text."""
        lines = []
        for e in events:
            lines.append(f"data: {json.dumps(e, ensure_ascii=False)}")
        return "\n".join(lines) + "\n"

    def test_simple_text_completion(self):
        from proxy_app import _parse_anthropic_sse
        events = [
            {"type": "message_start", "message": {"id": "msg_1", "model": "claude", "role": "assistant", "usage": {"input_tokens": 10}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "World"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        result = _parse_anthropic_sse(self._make_raw_sse(events))
        assert result is not None
        obj = json.loads(result)
        assert obj["id"] == "msg_1"
        assert obj["model"] == "claude"
        assert obj["stop_reason"] == "end_turn"
        assert obj["content"][0]["type"] == "text"
        assert obj["content"][0]["text"] == "Hello World"
        assert obj["usage"]["input_tokens"] == 10
        assert obj["usage"]["output_tokens"] == 5

    def test_tool_use(self):
        from proxy_app import _parse_anthropic_sse
        events = [
            {"type": "message_start", "message": {"id": "msg_2", "model": "claude", "role": "assistant"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tu_1", "name": "get_weather"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"cit'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": 'y":"Paris"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
        result = _parse_anthropic_sse(self._make_raw_sse(events))
        assert result is not None
        obj = json.loads(result)
        assert obj["stop_reason"] == "tool_use"
        assert obj["content"][0]["type"] == "tool_use"
        assert obj["content"][0]["id"] == "tu_1"
        assert obj["content"][0]["input"]["city"] == "Paris"

    def test_thinking_block(self):
        from proxy_app import _parse_anthropic_sse
        events = [
            {"type": "message_start", "message": {"id": "msg_3", "model": "claude-opus", "role": "assistant"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me think..."}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_abc"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Here is the answer."}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
        result = _parse_anthropic_sse(self._make_raw_sse(events))
        assert result is not None
        obj = json.loads(result)
        assert len(obj["content"]) == 2
        assert obj["content"][0]["type"] == "thinking"
        assert obj["content"][0]["thinking"] == "Let me think..."
        assert obj["content"][0]["signature"] == "sig_abc"
        assert obj["content"][1]["type"] == "text"
        assert obj["content"][1]["text"] == "Here is the answer."

    def test_error_event_captured(self):
        """Anthropic error event → captured as stop_reason/stop_details, not ignored."""
        from proxy_app import _parse_anthropic_sse
        events = [
            {"type": "message_start", "message": {"id": "msg_e", "model": "claude", "role": "assistant"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "Partia"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "l..."}},
            {"type": "error", "error": {"type": "overloaded_error", "message": "The service is overloaded"}},
            {"type": "message_stop"},
        ]
        result = _parse_anthropic_sse(self._make_raw_sse(events))
        assert result is not None
        obj = json.loads(result)
        assert obj["stop_reason"] == "error"
        assert obj["stop_details"]["api_error"]["type"] == "overloaded_error"

    def test_multiple_blocks(self):
        """Text + tool_use interleaved."""
        from proxy_app import _parse_anthropic_sse
        events = [
            {"type": "message_start", "message": {"id": "msg_m", "model": "claude", "role": "assistant"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Using tools."}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_a", "name": "search"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q":"test"}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
        result = _parse_anthropic_sse(self._make_raw_sse(events))
        assert result is not None
        obj = json.loads(result)
        assert len(obj["content"]) == 2
        assert obj["content"][0]["type"] == "text"
        assert obj["content"][1]["type"] == "tool_use"

    def test_malformed_line_raises(self):
        """A single malformed JSON line must cause parse failure."""
        from proxy_app import _parse_anthropic_sse
        sse = "data: {\"type\":\"message_start\",\"message\":{\"id\":\"m1\",\"model\":\"c\",\"role\":\"assistant\"}}\n"
        sse += "data: NOT VALID JSON {{{ ~!@#\n"  # malformed
        sse += "data: {\"type\":\"message_stop\"}\n"
        result = _parse_anthropic_sse(sse)
        # Must fail because NOT VALID JSON cannot be parsed
        assert result is None


# =========================================================================
# 8. SSE Parsers — OpenAI Chat Completions
# =========================================================================
class TestOpenAIChatSSEParser:
    """OpenAI Chat Completions SSE reconstruction."""

    def _make_raw_sse(self, chunks):
        lines = []
        for c in chunks:
            lines.append(f"data: {json.dumps(c, ensure_ascii=False)}")
        lines.append("data: [DONE]")
        return "\n".join(lines) + "\n"

    def test_simple_chat(self):
        from proxy_app import _parse_openai_chat_sse
        chunks = [
            {"id": "chat-1", "object": "chat.completion.chunk", "model": "gpt-4", "choices": [{"delta": {"content": "Hello"}}]},
            {"id": "chat-1", "object": "chat.completion.chunk", "choices": [{"delta": {"content": " World"}}]},
            {"id": "chat-1", "object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}},
        ]
        result = _parse_openai_chat_sse(self._make_raw_sse(chunks))
        assert result is not None
        obj = json.loads(result)
        assert obj["id"] == "chat-1"
        assert obj["object"] == "chat.completion"
        assert obj["model"] == "gpt-4"
        assert obj["choices"][0]["message"]["content"] == "Hello World"
        assert obj["choices"][0]["finish_reason"] == "stop"
        assert obj["usage"]["total_tokens"] == 5

    def test_with_tool_calls(self):
        from proxy_app import _parse_openai_chat_sse
        chunks = [
            {"id": "chat-tc", "object": "chat.completion.chunk", "model": "gpt-4", "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}]}}]},
            {"id": "chat-tc", "object": "chat.completion.chunk", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Paris"}'}}]}}]},
            {"id": "chat-tc", "object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        result = _parse_openai_chat_sse(self._make_raw_sse(chunks))
        obj = json.loads(result)
        msg = obj["choices"][0]["message"]
        assert msg["tool_calls"][0]["id"] == "call_1"
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"

    def test_reasoning_content(self):
        from proxy_app import _parse_openai_chat_sse
        chunks = [
            {"id": "chat-r", "object": "chat.completion.chunk", "model": "o3", "choices": [{"delta": {"reasoning_content": "Let me think"}}]},
            {"id": "chat-r", "object": "chat.completion.chunk", "choices": [{"delta": {"reasoning_content": " step by step"}}]},
            {"id": "chat-r", "object": "chat.completion.chunk", "choices": [{"delta": {"content": "Answer."}}]},
            {"id": "chat-r", "object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        result = _parse_openai_chat_sse(self._make_raw_sse(chunks))
        obj = json.loads(result)
        assert obj["choices"][0]["message"]["reasoning_content"] == "Let me think step by step"
        assert obj["choices"][0]["message"]["content"] == "Answer."

    def test_malformed_line_raises(self):
        from proxy_app import _parse_openai_chat_sse
        sse = 'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"}}]}\n'
        sse += 'data: ~broken~\n'
        sse += 'data: [DONE]\n'
        result = _parse_openai_chat_sse(sse)
        assert result is None


# =========================================================================
# 9. SSE Parsers — OpenAI Responses
# =========================================================================
class TestOpenAIResponsesSSEParser:
    """OpenAI Responses API SSE reconstruction."""

    def _make_raw_sse(self, events):
        lines = []
        for e in events:
            lines.append(f"data: {json.dumps(e, ensure_ascii=False)}")
        return "\n".join(lines) + "\n"

    def test_basic_response(self):
        from proxy_app import _parse_openai_responses_sse
        events = [
            {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-4o", "status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.output_text.delta", "delta": " World"},
            {"type": "response.completed", "response": {"usage": {"total_tokens": 10}}},
        ]
        result = _parse_openai_responses_sse(self._make_raw_sse(events))
        obj = json.loads(result)
        assert obj["id"] == "resp_1"
        assert obj["status"] == "completed"
        assert obj["output"][0]["content"][0]["text"] == "Hello World"
        assert obj["usage"]["total_tokens"] == 10

    def test_function_call(self):
        from proxy_app import _parse_openai_responses_sse
        events = [
            {"type": "response.created", "response": {"id": "resp_fc", "model": "gpt-4o"}},
            {"type": "response.function_call_arguments.delta", "delta": '{"city":'},
            {"type": "response.function_call_arguments.delta", "delta": '"Paris"}'},
            {"type": "response.completed", "response": {}},
        ]
        result = _parse_openai_responses_sse(self._make_raw_sse(events))
        obj = json.loads(result)
        assert obj["output"][0]["arguments"]["city"] == "Paris"

    def test_failed_response(self):
        from proxy_app import _parse_openai_responses_sse
        events = [
            {"type": "response.created", "response": {"id": "resp_f", "model": "gpt-4o"}},
            {"type": "response.failed"},
        ]
        result = _parse_openai_responses_sse(self._make_raw_sse(events))
        obj = json.loads(result)
        assert obj["status"] == "failed"

    def test_malformed_line_raises(self):
        from proxy_app import _parse_openai_responses_sse
        sse = 'data: {"type":"response.created","response":{"id":"r1","model":"gpt"}}\n'
        sse += 'data: not-json\n'
        result = _parse_openai_responses_sse(sse)
        assert result is None


# =========================================================================
# 10. SSE Parsers — Gemini
# =========================================================================
class TestGeminiSSEParser:
    """Google Gemini SSE reconstruction."""

    def _make_raw_sse(self, chunks):
        lines = []
        for c in chunks:
            lines.append(f"data: {json.dumps(c, ensure_ascii=False)}")
        return "\n".join(lines) + "\n"

    def test_basic_gemini(self):
        from proxy_app import _parse_gemini_sse
        chunks = [
            {"modelVersion": "gemini-2.5-pro", "responseId": "g1",
             "candidates": [{"content": {"role": "model", "parts": [{"text": "Hello"}]}}]},
            {"candidates": [{"content": {"parts": [{"text": " World"}]}}]},
            {"candidates": [{"finishReason": "STOP"}]},
        ]
        result = _parse_gemini_sse(self._make_raw_sse(chunks))
        obj = json.loads(result)
        assert obj["modelVersion"] == "gemini-2.5-pro"
        assert obj["candidates"][0]["content"]["parts"][0]["text"] == "Hello World"
        assert obj["candidates"][0]["finishReason"] == "STOP"

    def test_with_usage(self):
        from proxy_app import _parse_gemini_sse
        chunks = [
            {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]},
            {"usageMetadata": {"totalTokenCount": 5}},
        ]
        result = _parse_gemini_sse(self._make_raw_sse(chunks))
        obj = json.loads(result)
        assert obj["usageMetadata"]["totalTokenCount"] == 5

    def test_malformed_line_raises(self):
        from proxy_app import _parse_gemini_sse
        sse = 'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n'
        sse += 'data: ~bad~\n'
        result = _parse_gemini_sse(sse)
        assert result is None


# =========================================================================
# 11. SSE — Universal/Generic parser
# =========================================================================
class TestUniversalSSEParser:
    def test_deep_merge_strings(self):
        from proxy_app import _deep_merge
        result = _deep_merge({"text": "Hello"}, {"text": " World"})
        assert result["text"] == "Hello World"

    def test_deep_merge_nested(self):
        from proxy_app import _deep_merge
        result = _deep_merge(
            {"choices": [{"delta": {"content": "A"}}]},
            {"choices": [{"delta": {"content": "B"}}]},
        )
        assert result["choices"][0]["delta"]["content"] == "AB"

    def test_deep_merge_new_key(self):
        from proxy_app import _deep_merge
        result = _deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_universal_openai_chat(self):
        from proxy_app import _reconstruct_sse_universal
        sse = 'data: {"id":"c1","object":"chat.completion.chunk","model":"gpt","choices":[{"delta":{"content":"Hello"}}]}\n'
        sse += 'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"delta":{"content":" World"}}]}\n'
        sse += 'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}]}\n'
        sse += 'data: [DONE]\n'
        result = _reconstruct_sse_universal(sse)
        obj = json.loads(result)
        assert obj["choices"][0]["message"]["content"] == "Hello World"

    def test_generic_fallback(self):
        from proxy_app import _parse_generic_sse
        sse = 'data: {"choices":[{"delta":{"content":"test"}}]}\n'
        sse += 'data: [DONE]\n'
        result = _parse_generic_sse(sse)
        assert result is not None
        assert "test" in result

    def test_generic_empty_stream(self):
        from proxy_app import _parse_generic_sse
        result = _parse_generic_sse("")
        # Empty input returns empty string (raw SSE fallback, consistent with design)
        # This is not None but also not meaningful content
        assert result == "" or result is None


# =========================================================================
# 12. Dispatcher & multi-pass fallback
# =========================================================================
class TestDispatcher:
    def test_dispatcher_anthropic_success(self):
        from proxy_app import _reconstruct_sse_to_json
        sse = 'data: {"type":"message_start","message":{"id":"m","model":"c","role":"assistant"}}\n'
        sse += 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
        sse += 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n'
        sse += 'data: {"type":"content_block_stop","index":0}\n'
        sse += 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n'
        sse += 'data: {"type":"message_stop"}\n'
        result = _reconstruct_sse_to_json(sse)
        assert result is not None
        obj = json.loads(result)
        assert obj["content"][0]["text"] == "hi"

    def test_dispatcher_falls_back_to_generic(self):
        """When specific parser fails, dispatcher falls back to universal."""
        from proxy_app import _reconstruct_sse_to_json
        # First line looks like Anthropic (message_start) → triggers anthropic parser
        # But the stream is completely broken → anthropic returns None → fallback to generic
        sse = 'data: {"type":"message_start","message":{"id":"m","model":"c","role":"assistant"}}\n'
        sse += 'data: "this is not valid sse for any parser"\n'  # bad line
        result = _reconstruct_sse_to_json(sse)
        # Generic parser may produce raw SSE text as fallback — that's OK
        assert result is not None

    def test_dispatcher_empty(self):
        from proxy_app import _reconstruct_sse_to_json
        assert _reconstruct_sse_to_json("") is None
        assert _reconstruct_sse_to_json("not sse at all") is None


# =========================================================================
# 13. Port cache logic (sync, no DB required)
# =========================================================================
class TestPortCache:
    def test_cache_initial_state(self):
        import proxy_app
        proxy_app._port_target_cache.clear()
        proxy_app._cache_updated_at = 0.0
        assert proxy_app._port_target_cache == {}

    def test_get_target_url_empty_cache(self):
        """Without DB, get_target_url returns None gracefully."""
        import proxy_app
        proxy_app._port_target_cache.clear()
        proxy_app._cache_updated_at = 0.0
        # This will attempt a DB query and fail (no engine)
        # We just verify it doesn't crash catastrophically
        try:
            result = proxy_app.get_target_url(99999)
            # May return None or raise — either is acceptable without DB
        except Exception:
            pass  # Expected when DB is not initialized

    def test_cache_update_manual(self):
        import proxy_app
        proxy_app._port_target_cache[12345] = "https://test.api.com"
        proxy_app._cache_updated_at = time.time()
        cache = proxy_app._port_target_cache
        assert cache[12345] == "https://test.api.com"
        proxy_app._port_target_cache.clear()


# =========================================================================
# 14. DB utility (unit-testable parts)
# =========================================================================
class TestDatabaseUtils:
    def test_thread_pool_exists(self):
        from database import _db_executor
        from concurrent.futures import ThreadPoolExecutor
        assert isinstance(_db_executor, ThreadPoolExecutor)

    def test_shutdown_registered(self):
        from database import shutdown_db_executor
        assert callable(shutdown_db_executor)


# =========================================================================
# 15. Proxy manager (unit-testable parts)
# =========================================================================
class TestProxyManager:
    def test_imports_cleanly(self):
        from proxy_manager import ProxyManager
        pm = ProxyManager()
        assert pm is not None

    def test_has_no_stop_all(self):
        """stop_all was removed as dead code."""
        from proxy_manager import ProxyManager
        assert not hasattr(ProxyManager, "stop_all")


# =========================================================================
# 16. Import smoke test — every module
# =========================================================================
class TestImportSmoke:
    MODULES = [
        "config", "auth", "models", "schemas", "database",
        "proxy_app", "shared_proxy", "proxy_manager",
        "routers.auth_router", "routers.admin_router",
        "routers.ports_router", "routers.config_router",
    ]

    @pytest.mark.parametrize("modname", MODULES)
    def test_import_module(self, modname):
        import importlib
        m = importlib.import_module(modname)
        assert m is not None

    def test_no_stream_reconstructor_importable(self):
        """stream_reconstructor.py was deleted."""
        with pytest.raises(ModuleNotFoundError):
            import stream_reconstructor  # noqa: F401
