"""
Tests for proxy forwarding and SSE reconstruction.
Tests the SSE parsing logic directly and the proxy endpoint via ASGI.
"""
import json
import pytest
import pytest_asyncio


# ---- SSE Reconstruction Tests ----

class TestSSEReconstruction:
    """Test the SSE-to-JSON reconstruction logic directly."""

    def test_anthropic_sse_parsing(self):
        """Parse Anthropic Messages API SSE stream."""
        from proxy_app import _parse_anthropic_sse

        raw_sse = (
            'event: message_start\n'
            'data: {"type":"message_start","message":{"id":"msg_123","type":"message","role":"assistant","content":[],"model":"claude-3","stop_reason":null,"usage":{"input_tokens":10}}}\n\n'
            'event: content_block_start\n'
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            'event: content_block_delta\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n'
            'event: content_block_delta\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world!"}}\n\n'
            'event: content_block_stop\n'
            'data: {"type":"content_block_stop","index":0}\n\n'
            'event: message_delta\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
            'event: message_stop\n'
            'data: {"type":"message_stop"}\n\n'
        )

        result = _parse_anthropic_sse(raw_sse)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["id"] == "msg_123"
        assert parsed["role"] == "assistant"
        assert len(parsed["content"]) == 1
        assert parsed["content"][0]["type"] == "text"
        assert parsed["content"][0]["text"] == "Hello world!"
        assert parsed["stop_reason"] == "end_turn"
        assert parsed["usage"]["input_tokens"] == 10
        assert parsed["usage"]["output_tokens"] == 5

    def test_openai_chat_sse_parsing(self):
        """Parse OpenAI Chat Completions SSE stream."""
        from proxy_app import _parse_openai_chat_sse

        raw_sse = (
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"content":" world!"},"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":10,"total_tokens":15}}\n\n'
            'data: [DONE]\n\n'
        )

        result = _parse_openai_chat_sse(raw_sse)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["id"] == "chatcmpl-123"
        assert parsed["model"] == "gpt-4"
        assert len(parsed["choices"]) == 1
        assert parsed["choices"][0]["message"]["content"] == "Hello world!"
        assert parsed["choices"][0]["finish_reason"] == "stop"
        assert parsed["usage"]["total_tokens"] == 15

    def test_openai_responses_sse_parsing(self):
        """Parse OpenAI Responses API SSE stream."""
        from proxy_app import _parse_openai_responses_sse

        raw_sse = (
            'data: {"type":"response.created","response":{"id":"resp_123","model":"gpt-4","status":"in_progress"}}\n\n'
            'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
            'data: {"type":"response.output_text.delta","delta":" world!"}\n\n'
            'data: {"type":"response.completed","response":{"id":"resp_123","status":"completed","usage":{"input_tokens":5,"output_tokens":10}}}\n\n'
        )

        result = _parse_openai_responses_sse(raw_sse)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["id"] == "resp_123"
        assert parsed["status"] == "completed"

    def test_gemini_sse_parsing(self):
        """Parse Google Gemini SSE stream."""
        from proxy_app import _parse_gemini_sse

        raw_sse = (
            'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Hello"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":10}}\n\n'
            'data: {"candidates":[{"content":{"role":"model","parts":[{"text":" world!"}]},"finishReason":"STOP"}]}\n\n'
        )

        result = _parse_gemini_sse(raw_sse)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["candidates"]) == 1
        assert parsed["candidates"][0]["content"]["parts"][0]["text"] == "Hello world!"
        assert parsed["candidates"][0]["finishReason"] == "STOP"

    def test_sse_format_detection(self):
        """Auto-detect SSE format from first chunk."""
        from proxy_app import _detect_sse_format

        # Anthropic
        assert _detect_sse_format({"type": "message_start"}) == "anthropic"
        assert _detect_sse_format({"type": "content_block_start"}) == "anthropic"

        # OpenAI Responses
        assert _detect_sse_format({"type": "response.created"}) == "openai_responses"
        assert _detect_sse_format({"type": "response.output_text.delta"}) == "openai_responses"

        # OpenAI Chat
        assert _detect_sse_format({"choices": [{"delta": {"content": "hi"}}]}) == "openai_chat"

        # Gemini
        assert _detect_sse_format({"candidates": []}) == "gemini"

        # Generic
        assert _detect_sse_format({"unknown": "format"}) == "generic"

    def test_anthropic_thinking_blocks(self):
        """Parse Anthropic SSE with thinking blocks."""
        from proxy_app import _parse_anthropic_sse

        raw_sse = (
            'data: {"type":"message_start","message":{"id":"msg_think","role":"assistant","content":[],"model":"claude-3"}}\n\n'
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Let me think..."}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig123"}}\n\n'
            'data: {"type":"content_block_stop","index":0}\n\n'
            'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n'
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Here is my answer."}}\n\n'
            'data: {"type":"content_block_stop","index":1}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        )

        result = _parse_anthropic_sse(raw_sse)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["content"]) == 2
        assert parsed["content"][0]["type"] == "thinking"
        assert parsed["content"][0]["thinking"] == "Let me think..."
        assert parsed["content"][1]["type"] == "text"
        assert parsed["content"][1]["text"] == "Here is my answer."

    def test_anthropic_tool_use_blocks(self):
        """Parse Anthropic SSE with tool_use blocks."""
        from proxy_app import _parse_anthropic_sse

        raw_sse = (
            'data: {"type":"message_start","message":{"id":"msg_tool","role":"assistant","content":[],"model":"claude-3"}}\n\n'
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_123","name":"get_weather"}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"locati"}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"on\\": \\"NYC\\"}"}}\n\n'
            'data: {"type":"content_block_stop","index":0}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        )

        result = _parse_anthropic_sse(raw_sse)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["content"]) == 1
        assert parsed["content"][0]["type"] == "tool_use"
        assert parsed["content"][0]["name"] == "get_weather"
        assert parsed["content"][0]["input"]["location"] == "NYC"

    def test_openai_tool_calls_sse(self):
        """Parse OpenAI SSE with tool calls."""
        from proxy_app import _parse_openai_chat_sse

        raw_sse = (
            'data: {"id":"chatcmpl-tc","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-tc","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_123","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-tc","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"loc"}}]},"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-tc","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ation\\":\\"NYC\\"}"}}]},"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-tc","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            'data: [DONE]\n\n'
        )

        result = _parse_openai_chat_sse(raw_sse)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["choices"][0]["message"]["tool_calls"]) == 1
        tc = parsed["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "get_weather"
        assert "NYC" in tc["function"]["arguments"]


# ---- Serialization Tests ----

class TestSerialization:
    """Test body serialization and helper functions."""

    def test_serialize_body_json(self):
        """Serialize JSON body."""
        from proxy_app import _serialize_body
        result = _serialize_body(b'{"key": "value"}')
        assert result is not None
        assert '"key"' in result

    def test_serialize_body_text(self):
        """Serialize plain text body."""
        from proxy_app import _serialize_body
        result = _serialize_body(b'hello world')
        assert result == "hello world"

    def test_serialize_body_empty(self):
        """Serialize empty body."""
        from proxy_app import _serialize_body
        assert _serialize_body(b"") is None

    def test_serialize_body_binary(self):
        """Serialize binary data."""
        from proxy_app import _serialize_body
        result = _serialize_body(bytes(range(256)))
        assert "binary data" in result

    def test_deep_merge_dicts(self):
        """Deep merge two dictionaries."""
        from proxy_app import _deep_merge
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        chunk = {"b": {"c": 10, "e": 4}, "f": 5}
        result = _deep_merge(base, chunk)
        assert result["a"] == 1
        assert result["b"]["c"] == 10  # ints overwrite
        assert result["b"]["d"] == 3
        assert result["b"]["e"] == 4
        assert result["f"] == 5

    def test_deep_merge_strings_concatenate(self):
        """Deep merge concatenates strings."""
        from proxy_app import _deep_merge
        base = {"text": "Hello"}
        chunk = {"text": " world"}
        result = _deep_merge(base, chunk)
        assert result["text"] == "Hello world"


# ---- Health Endpoint ----

@pytest.mark.asyncio
async def test_health_endpoint(client):
    """Health check endpoint works."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---- Port Cache Tests ----

class TestPortCache:
    """Test port target URL caching."""

    def test_cache_hit(self):
        """Cache returns cached value."""
        from proxy_app import _port_target_cache, get_target_url
        _port_target_cache[9999] = "https://cached.example.com"
        result = get_target_url(9999)
        assert result == "https://cached.example.com"
        del _port_target_cache[9999]

    def test_cache_miss_returns_none(self):
        """Cache miss for non-existent port returns None."""
        from proxy_app import get_target_url
        result = get_target_url(49999)
        assert result is None
