"""
SSE stream parsers — LiteLLM-inspired three-layer architecture.

Layer 1: Provider-specific parsers convert raw SSE lines → StreamChunk
Layer 2: StreamChunk — unified intermediate representation
Layer 3: parser.finalize() — assemble StreamChunks → reconstructed JSON

Each parser class has a ``parse()`` classmethod that takes raw SSE text and
returns a JSON string (or None), matching the old per-provider function
signatures for drop-in compatibility.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("llm_proxy.sse_parsers")

# ──────────────────────────────────────────────
#  Format detection constants
# ──────────────────────────────────────────────

# Anthropic Messages API event types (including error and ping for detection)
_ANTHROPIC_EVENT_TYPES: frozenset[str] = frozenset({
    "message_start", "content_block_start", "content_block_delta",
    "content_block_stop", "message_delta", "message_stop",
    "ping", "error",
})

# OpenAI Responses API event type prefixes
_OPENAI_RESPONSES_PREFIXES: tuple[str, ...] = (
    "response.", "error",
)


# ──────────────────────────────────────────────
#  Layer 2: Unified intermediate representation
# ──────────────────────────────────────────────

@dataclass(slots=True)
class StreamChunk:
    """Normalized representation of one parsed SSE data line.

    Analogous to LiteLLM's ``ModelResponseStream`` — a provider-agnostic
    intermediate format that every parser produces.
    """
    text_delta: str = ""
    reasoning_delta: str = ""
    tool_call_delta: dict | None = None   # {index, id?, name?, arguments?}
    finish_reason: str | None = None
    usage: dict | None = None
    metadata: dict = field(default_factory=dict)  # {model, id, object, role, ...}
    raw: dict | None = None  # original parsed JSON for provider-specific finalize()


# ──────────────────────────────────────────────
#  Layer 3 helper: ChunkAccumulator
# ──────────────────────────────────────────────

class ChunkAccumulator:
    """Collect StreamChunk objects and merge by field type.

    Analogous to LiteLLM's ``ChunkProcessor`` / ``stream_chunk_builder``,
    but simpler — no audio/images/annotations handling needed here.
    """

    def __init__(self) -> None:
        self._text: str = ""
        self._reasoning: str = ""
        self._tool_calls: dict[int, dict] = {}    # index → {id, type, function: {name, arguments}}
        self._finish_reason: str | None = None
        self._usage: dict | None = None
        self._metadata: dict = {}

    # -- mutation --

    def add(self, chunk: StreamChunk) -> None:
        """Merge one chunk into the accumulator."""
        if chunk.text_delta:
            self._text += chunk.text_delta
        if chunk.reasoning_delta:
            self._reasoning += chunk.reasoning_delta

        # Tool calls merge by index
        if chunk.tool_call_delta:
            idx = chunk.tool_call_delta.get("index", 0)
            tc = self._tool_calls.setdefault(idx, {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if chunk.tool_call_delta.get("id"):
                tc["id"] = chunk.tool_call_delta["id"]
            if chunk.tool_call_delta.get("type"):
                tc["type"] = chunk.tool_call_delta["type"]
            if chunk.tool_call_delta.get("name"):
                tc["function"]["name"] += chunk.tool_call_delta["name"]
            if chunk.tool_call_delta.get("arguments"):
                tc["function"]["arguments"] += chunk.tool_call_delta["arguments"]

        if chunk.finish_reason:
            self._finish_reason = chunk.finish_reason

        if chunk.usage:
            if self._usage is None:
                self._usage = {}
            self._usage.update(chunk.usage)

        if chunk.metadata and not self._metadata:
            self._metadata = dict(chunk.metadata)

    # -- read-only properties --

    @property
    def text(self) -> str:
        return self._text

    @property
    def reasoning(self) -> str:
        return self._reasoning

    @property
    def tool_calls(self) -> dict[int, dict]:
        return self._tool_calls

    @property
    def finish_reason(self) -> str | None:
        return self._finish_reason

    @property
    def usage(self) -> dict | None:
        return self._usage

    @property
    def metadata(self) -> dict:
        return self._metadata


# ──────────────────────────────────────────────
#  Layer 1: Base parser
# ──────────────────────────────────────────────

class BaseSSEParser(ABC):
    """Stateful parser: one SSE line at a time → StreamChunk.

    Analogous to LiteLLM's ``BaseModelResponseIterator`` — each provider
    subclass implements ``parse_line()`` and ``finalize()``.
    """

    @abstractmethod
    def parse_line(self, line: str) -> StreamChunk | None:
        """Parse one SSE data line.

        Returns None for non-data lines (event lines, comments, blanks, [DONE]).
        """
        ...

    @abstractmethod
    def finalize(self) -> dict | None:
        """Build the final reconstructed response dict from accumulated state."""
        ...

    @classmethod
    def parse(cls, raw_sse: str) -> str | None:
        """Convenience: parse full SSE text → reconstructed JSON string.

        Equivalent to the old ``_parse_xxx_sse(raw_sse)`` functions.
        """
        parser = cls()
        try:
            for line in raw_sse.split("\n"):
                parser.parse_line(line.rstrip("\r"))
            result = parser.finalize()
            if result is None:
                return None
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning(
                "%s.parse() failed", cls.__name__, exc_info=True,
            )
            return None


# ──────────────────────────────────────────────
#  Provider: Anthropic Messages API
# ──────────────────────────────────────────────

class AnthropicSSEParser(BaseSSEParser):
    """Parse Anthropic Messages API SSE stream → reconstructed Messages JSON.

    Handles: text, thinking (extended thinking), tool_use,
    server_tool_use, search_tool, and compaction blocks.

    Uses a state machine driven by Anthropic's multi-event SSE protocol:
      message_start → content_block_start → content_block_delta →
      content_block_stop → message_delta → message_stop
    """

    # Event type constants
    _MSG_START = "message_start"
    _CB_START = "content_block_start"
    _CB_DELTA = "content_block_delta"
    _CB_STOP = "content_block_stop"
    _MSG_DELTA = "message_delta"
    _MSG_STOP = "message_stop"

    # Delta types for content_block_delta
    _DELTA_TEXT = "text_delta"
    _DELTA_THINKING = "thinking_delta"
    _DELTA_SIGNATURE = "signature_delta"
    _DELTA_INPUT_JSON = "input_json_delta"
    _DELTA_CITATIONS = "citations_delta"
    _DELTA_COMPACTION = "compaction_delta"

    # Content block types
    _BLOCK_TEXT = "text"
    _BLOCK_TOOL_USE = "tool_use"
    _BLOCK_SERVER_TOOL_USE = "server_tool_use"
    _BLOCK_THINKING = "thinking"
    _BLOCK_COMPACTION = "compaction"
    _BLOCK_SEARCH_TOOL = "search_tool"

    def __init__(self) -> None:
        self.blocks: dict[int, dict] = {}
        # Each block: {"type": str, "text": str, "thinking": str, "signature": str,
        #               "id": str, "name": str, "input_json": str}
        self.stop_reason: str | None = None
        self.stop_sequence: str | None = None
        self.stop_details: dict | None = None
        self.usage_output: dict | None = None
        self.usage_input: dict | None = None
        self.model: str = ""
        self.msg_id: str = ""
        self.role: str = "assistant"

    def parse_line(self, line: str) -> StreamChunk | None:
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload:
            return None

        event = json.loads(payload)
        event_type = event.get("type", "")

        # ── message_start ──
        if event_type == self._MSG_START:
            message = event.get("message", {})
            self.msg_id = message.get("id", "")
            self.model = message.get("model", "")
            self.role = message.get("role", "assistant")
            if message.get("usage"):
                self.usage_input = message["usage"]

        # ── content_block_start ──
        elif event_type == self._CB_START:
            idx = event.get("index", 0)
            cb = event.get("content_block", {})
            block_type = cb.get("type", "text")

            self.blocks[idx] = {"type": block_type}

            if block_type in (self._BLOCK_TOOL_USE, self._BLOCK_SERVER_TOOL_USE):
                self.blocks[idx].update({
                    "id": cb.get("id", ""),
                    "name": cb.get("name", ""),
                    "input_json": "",
                })
            elif block_type == self._BLOCK_THINKING:
                self.blocks[idx].update({
                    "thinking": "",
                    "signature": "",
                })
            elif block_type == self._BLOCK_COMPACTION:
                self.blocks[idx].update({
                    "content": "",
                    "encrypted_content": "",
                })
            elif block_type == self._BLOCK_TEXT:
                self.blocks[idx]["text"] = cb.get("text", "")
                self.blocks[idx]["citations"] = []
            elif block_type in (self._BLOCK_SEARCH_TOOL,):
                self.blocks[idx].update({
                    "id": cb.get("id", ""),
                    "name": cb.get("name", ""),
                    "input_json": "",
                })
            else:
                # Unknown future block type — accumulate as generic text
                self.blocks[idx]["_text"] = cb.get("text", "")

        # ── content_block_delta ──
        elif event_type == self._CB_DELTA:
            idx = event.get("index", 0)
            delta = event.get("delta", {})
            delta_type = delta.get("type", "")

            if idx not in self.blocks:
                # content_block_start was missed — infer type from delta type
                if delta_type == self._DELTA_INPUT_JSON:
                    self.blocks[idx] = {"type": self._BLOCK_TOOL_USE, "id": "", "name": "", "input_json": ""}
                elif delta_type == self._DELTA_THINKING:
                    self.blocks[idx] = {"type": self._BLOCK_THINKING, "thinking": "", "signature": ""}
                elif delta_type == self._DELTA_SIGNATURE:
                    self.blocks[idx] = {"type": self._BLOCK_THINKING, "thinking": "", "signature": ""}
                elif delta_type == self._DELTA_COMPACTION:
                    self.blocks[idx] = {"type": self._BLOCK_COMPACTION, "content": "", "encrypted_content": ""}
                else:
                    self.blocks[idx] = {"type": self._BLOCK_TEXT, "text": "", "citations": []}

            if delta_type == self._DELTA_TEXT:
                self.blocks[idx]["text"] = self.blocks[idx].get("text", "") + delta.get("text", "")
            elif delta_type == self._DELTA_THINKING:
                self.blocks[idx]["thinking"] = self.blocks[idx].get("thinking", "") + delta.get("thinking", "")
            elif delta_type == self._DELTA_SIGNATURE:
                self.blocks[idx]["signature"] = self.blocks[idx].get("signature", "") + delta.get("signature", "")
            elif delta_type == self._DELTA_INPUT_JSON:
                self.blocks[idx]["input_json"] = (
                    self.blocks[idx].get("input_json", "")
                    + delta.get("partial_json", "")
                )
            elif delta_type == self._DELTA_CITATIONS:
                if "citations" not in self.blocks[idx]:
                    self.blocks[idx]["citations"] = []
                if delta.get("citation"):
                    self.blocks[idx]["citations"].append(delta["citation"])
            elif delta_type == self._DELTA_COMPACTION:
                self.blocks[idx]["content"] = delta.get("content", "")
                self.blocks[idx]["encrypted_content"] = delta.get("encrypted_content", "")
            else:
                # Unknown delta type — concatenate all string values as text
                for dk, dv in delta.items():
                    if dk != "type" and isinstance(dv, str):
                        self.blocks[idx].setdefault("text", "")
                        self.blocks[idx]["text"] += dv

        # ── content_block_stop ── (no data needed, index is enough)
        elif event_type == self._CB_STOP:
            pass

        # ── message_delta ──
        elif event_type == self._MSG_DELTA:
            delta = event.get("delta", {})
            if delta.get("stop_reason"):
                self.stop_reason = delta["stop_reason"]
            if delta.get("stop_sequence") is not None:
                self.stop_sequence = delta["stop_sequence"]
            if delta.get("stop_details") is not None:
                self.stop_details = delta["stop_details"]
            if event.get("usage"):
                self.usage_output = event["usage"]

        # ── message_stop ── (end of stream, no data)
        elif event_type == self._MSG_STOP:
            pass

        # ── error ── (upstream API reported an error — capture it)
        elif event_type == "error":
            err_info = event.get("error", {})
            self.stop_reason = "error"
            self.stop_details = {"api_error": err_info}
            logger.warning(
                "Anthropic SSE stream contains error event: %s",
                err_info,
            )

        return StreamChunk(raw=event)

    def finalize(self) -> dict | None:
        # Build content blocks
        content_blocks: list[dict] = []
        for _idx in sorted(self.blocks.keys()):
            b = self.blocks[_idx]
            block_type = b.get("type", "text")

            if block_type == self._BLOCK_TEXT:
                text_block: dict = {"type": "text", "text": b.get("text", "")}
                if b.get("citations"):
                    text_block["citations"] = b["citations"]
                content_blocks.append(text_block)

            elif block_type == self._BLOCK_THINKING:
                content_blocks.append({
                    "type": "thinking",
                    "thinking": b.get("thinking", ""),
                    "signature": b.get("signature", ""),
                })

            elif block_type == self._BLOCK_COMPACTION:
                content_blocks.append({
                    "type": "compaction",
                    "content": b.get("content", ""),
                    "encrypted_content": b.get("encrypted_content", ""),
                })

            elif block_type in (self._BLOCK_TOOL_USE, self._BLOCK_SERVER_TOOL_USE, self._BLOCK_SEARCH_TOOL):
                tool_block: dict = {
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
                plain: dict = {"type": block_type}
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
        if self.usage_input or self.usage_output:
            usage = {}
            if self.usage_input:
                usage.update(self.usage_input)
            if self.usage_output:
                usage.update(self.usage_output)

        reconstructed: dict = {
            "id": self.msg_id,
            "type": "message",
            "role": self.role,
            "content": content_blocks,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "stop_sequence": self.stop_sequence,
        }
        if self.stop_details is not None:
            reconstructed["stop_details"] = self.stop_details
        if usage:
            reconstructed["usage"] = usage

        return reconstructed


# ──────────────────────────────────────────────
#  Provider: OpenAI Chat Completions
# ──────────────────────────────────────────────

class OpenAIChatSSEParser(BaseSSEParser):
    """Parse OpenAI Chat Completions SSE stream → reconstructed chat.completion JSON.

    Handles: choices[0].delta.content, reasoning_content, tool_calls.
    """

    def __init__(self) -> None:
        self._acc = ChunkAccumulator()

    def parse_line(self, line: str) -> StreamChunk | None:
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None

        chunk = json.loads(payload)
        chunk_data = StreamChunk(raw=chunk)

        # Metadata — capture once
        if not self._acc.metadata:
            chunk_data.metadata = {
                "id": chunk.get("id", ""),
                "object": chunk.get("object", "").replace(".chunk", ""),
                "model": chunk.get("model", ""),
                "created": chunk.get("created", 0),
            }

        if chunk.get("usage"):
            chunk_data.usage = chunk["usage"]

        choices = chunk.get("choices", [])
        if not choices:
            self._acc.add(chunk_data)
            return chunk_data

        delta = choices[0].get("delta", {})
        if delta.get("content"):
            chunk_data.text_delta = delta["content"]
        if delta.get("reasoning_content"):
            chunk_data.reasoning_delta = delta["reasoning_content"]

        # Streaming tool calls
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            chunk_data.tool_call_delta = {
                "index": idx,
                "id": tc.get("id", ""),
                "type": tc.get("type", "function"),
                "name": tc.get("function", {}).get("name", ""),
                "arguments": tc.get("function", {}).get("arguments", ""),
            }

        if choices[0].get("finish_reason") and not self._acc.finish_reason:
            chunk_data.finish_reason = choices[0]["finish_reason"]

        self._acc.add(chunk_data)
        return chunk_data

    def finalize(self) -> dict | None:
        meta = self._acc.metadata
        message: dict = {"role": "assistant", "content": self._acc.text}
        if self._acc.reasoning:
            message["reasoning_content"] = self._acc.reasoning
        if self._acc.tool_calls:
            message["tool_calls"] = [
                self._acc.tool_calls[i]
                for i in sorted(self._acc.tool_calls.keys())
            ]

        reconstructed: dict = {
            "id": meta.get("id", ""),
            "object": meta.get("object") or "chat.completion",
            "model": meta.get("model", ""),
            "created": meta.get("created", 0),
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self._acc.finish_reason or "stop",
            }],
        }
        if self._acc.usage:
            reconstructed["usage"] = self._acc.usage

        return reconstructed


# ──────────────────────────────────────────────
#  Provider: OpenAI Responses API
# ──────────────────────────────────────────────

class OpenAIResponsesSSEParser(BaseSSEParser):
    """Parse OpenAI Responses API SSE stream → reconstructed response JSON.

    Key events: response.output_text.delta, response.reasoning_summary_text.delta,
    response.function_call_arguments.delta, response.created, response.completed.
    """

    def __init__(self) -> None:
        self.output_text: str = ""
        self.reasoning_text: str = ""
        self.function_args: str = ""
        self.response_id: str = ""
        self.model: str = ""
        self.status: str = ""
        self.usage: dict | None = None
        self.output_items: list[dict] = []

    def parse_line(self, line: str) -> StreamChunk | None:
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None

        event = json.loads(payload)
        event_type = event.get("type", "")
        chunk = StreamChunk(raw=event)

        if event_type == "response.created":
            resp = event.get("response", {})
            self.response_id = resp.get("id", "")
            self.model = resp.get("model", "")
            self.status = resp.get("status", "")

        elif event_type == "response.completed":
            resp = event.get("response", {})
            if resp.get("usage"):
                self.usage = resp["usage"]
            self.status = "completed"
            if resp.get("output"):
                self.output_items = resp["output"]

        elif event_type == "response.output_text.delta":
            self.output_text += event.get("delta", "")
            chunk.text_delta = event.get("delta", "")

        elif event_type == "response.reasoning_summary_text.delta":
            self.reasoning_text += event.get("delta", "")
            chunk.reasoning_delta = event.get("delta", "")

        elif event_type == "response.function_call_arguments.delta":
            self.function_args += event.get("delta", "")

        elif event_type == "response.failed":
            self.status = "failed"

        return chunk

    def finalize(self) -> dict | None:
        reconstructed: dict = {
            "id": self.response_id,
            "object": "response",
            "model": self.model,
            "status": self.status or "completed",
        }

        # If we captured output items from response.completed, use those
        if self.output_items:
            reconstructed["output"] = self.output_items
        else:
            # Build output from accumulated deltas
            output: list[dict] = []
            if self.output_text:
                output.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self.output_text}],
                })
            if self.reasoning_text:
                output.append({
                    "type": "reasoning",
                    "summary": [{"type": "reasoning_summary_text", "text": self.reasoning_text}],
                })
            if self.function_args:
                try:
                    parsed_args = json.loads(self.function_args)
                except json.JSONDecodeError:
                    parsed_args = self.function_args
                output.append({
                    "type": "function_call",
                    "arguments": parsed_args,
                })
            reconstructed["output"] = output

        if self.usage:
            reconstructed["usage"] = self.usage

        return reconstructed


# ──────────────────────────────────────────────
#  Provider: Google Gemini
# ──────────────────────────────────────────────

class GeminiSSEParser(BaseSSEParser):
    """Parse Google Gemini SSE stream → reconstructed generateContent JSON.

    Each SSE chunk is a full GenerateContentResponse. We collect the text from
    candidates[0].content.parts[*].text across all chunks (Gemini sends
    incremental text in each chunk).
    """

    def __init__(self) -> None:
        self._acc = ChunkAccumulator()
        self._role: str = "model"
        self._response_id: str = ""

    def parse_line(self, line: str) -> StreamChunk | None:
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload:
            return None

        chunk = json.loads(payload)
        chunk_data = StreamChunk(raw=chunk)

        # Metadata — capture once
        if not self._acc.metadata:
            chunk_data.metadata = {
                "modelVersion": chunk.get("modelVersion", ""),
                "responseId": chunk.get("responseId", ""),
            }
            self._response_id = chunk.get("responseId", "")

        if chunk.get("usageMetadata"):
            chunk_data.usage = chunk["usageMetadata"]

        candidates = chunk.get("candidates", [])
        if not candidates:
            self._acc.add(chunk_data)
            return chunk_data

        candidate = candidates[0]
        content = candidate.get("content", {})
        self._role = content.get("role", "model")
        parts = content.get("parts", [])

        for part in parts:
            if "text" in part:
                chunk_data.text_delta = part["text"]

        if candidate.get("finishReason") and not self._acc.finish_reason:
            chunk_data.finish_reason = candidate["finishReason"]

        self._acc.add(chunk_data)
        return chunk_data

    def finalize(self) -> dict | None:
        meta = self._acc.metadata
        reconstructed: dict = {
            "responseId": meta.get("responseId", self._response_id),
            "modelVersion": meta.get("modelVersion", ""),
            "candidates": [{
                "index": 0,
                "content": {
                    "role": self._role,
                    "parts": [{"text": self._acc.text}],
                },
                "finishReason": self._acc.finish_reason or "STOP",
            }],
        }
        if self._acc.usage:
            reconstructed["usageMetadata"] = self._acc.usage

        return reconstructed


# ──────────────────────────────────────────────
#  Utilities: deep merge & text walk
# ──────────────────────────────────────────────

def deep_merge(base: dict, chunk: dict) -> dict:
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
                result[key] = deep_merge(old_val, new_val)
            elif isinstance(old_val, list) and isinstance(new_val, list):
                result[key] = merge_lists(old_val, new_val)
            else:
                # Overwrite with new value (for numbers, booleans, null)
                result[key] = new_val
    return result


def merge_lists(base_list: list, new_list: list) -> list:
    """Merge lists by index. If new_list has items at same indices, deep merge them."""
    result = list(base_list)
    for i, item in enumerate(new_list):
        if i < len(result):
            if isinstance(result[i], dict) and isinstance(item, dict):
                result[i] = deep_merge(result[i], item)
            elif isinstance(result[i], str) and isinstance(item, str):
                result[i] = result[i] + item
            else:
                result[i] = item
        else:
            result.append(item)
    return result


def walk_json_for_text(obj, depth: int = 0) -> str:
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
            pieces.append(walk_json_for_text(val, depth + 1))
        return "".join(pieces)
    if isinstance(obj, list):
        return "".join(walk_json_for_text(item, depth + 1) for item in obj)
    return ""


# ──────────────────────────────────────────────
#  Provider: Generic / Unknown (best-effort)
# ──────────────────────────────────────────────

class GenericSSEParser(BaseSSEParser):
    """Best-effort SSE parser for unknown formats.

    Strategy:
    1. Try universal deep merge (works for OpenAI-like formats)
    2. If that fails or produces empty content, fall back to text extraction
    3. If nothing works, return raw SSE text
    """

    def __init__(self) -> None:
        self._merged: dict = {}
        self._raw_lines: list[str] = []

    def parse_line(self, line: str) -> StreamChunk | None:
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None

        chunk = json.loads(payload)
        self._raw_lines.append(payload)

        # Accumulate via deep merge
        try:
            self._merged = deep_merge(self._merged, chunk)
        except Exception:
            # deep merge failed for this line; skip and continue
            pass

        return StreamChunk(raw=chunk)

    def finalize(self) -> dict | None:
        # Strategy 1: universal deep merge
        merged_result = None
        if self._merged:
            # Post-processing for OpenAI-like formats
            # Rename delta → message in choices (OpenAI Chat Completions pattern)
            for choice in self._merged.get("choices", []):
                if "delta" in choice:
                    choice["message"] = choice.pop("delta")

            merged_json = json.dumps(self._merged, ensure_ascii=False, indent=2)
            if merged_json != "{}":
                # Check if the merged result has meaningful content
                if self._merged.get("choices") or self._merged.get("candidates"):
                    merged_result = self._merged

        if merged_result is not None:
            return merged_result

        # Strategy 2: extract text from common paths
        all_text = ""
        for raw_line in self._raw_lines:
            obj = json.loads(raw_line)

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
                all_text += walk_json_for_text(obj)

        # Strategy 2 succeeded — return simple reconstructed object
        if all_text.strip():
            return {
                "_format": "generic",
                "content": all_text,
                "note": "Best-effort reconstruction — see response_body_raw for original SSE",
            }

        # Strategy 3: nothing found — signal caller to return raw SSE
        return None

    @classmethod
    def parse(cls, raw_sse: str) -> str | None:
        """Override to handle the 3-tier fallback strategy."""
        parser = cls()
        try:
            for line in raw_sse.split("\n"):
                parser.parse_line(line.rstrip("\r"))
            result = parser.finalize()
            if result is not None:
                return json.dumps(result, ensure_ascii=False, indent=2)
            # Strategy 3: return raw SSE so it's at least visible
            return raw_sse
        except Exception:
            logger.warning(
                "GenericSSEParser.parse() failed", exc_info=True,
            )
            return raw_sse


# ──────────────────────────────────────────────
#  Format detection (standalone function)
# ──────────────────────────────────────────────

def detect_sse_format(first_chunk: dict) -> str:
    """Detect the SSE stream format from the first parsed JSON chunk.

    Returns one of:
      'anthropic'        — Anthropic Messages API
      'openai_responses' — OpenAI Responses API  (/v1/responses)
      'gemini'           — Google Gemini API
      'openai_chat'      — OpenAI Chat Completions  (/v1/chat/completions)
      'generic'          — Unknown format, try best-effort extraction
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
#  Parser registry + dispatcher
# ──────────────────────────────────────────────

_PARSER_CLASSES: dict[str, type[BaseSSEParser]] = {
    "anthropic": AnthropicSSEParser,
    "openai_chat": OpenAIChatSSEParser,
    "openai_responses": OpenAIResponsesSSEParser,
    "gemini": GeminiSSEParser,
    "generic": GenericSSEParser,
}


def reconstruct_sse_to_json(raw_sse: str) -> str | None:
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
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            first_chunk = json.loads(payload)
            fmt = detect_sse_format(first_chunk)
            parser_cls = _PARSER_CLASSES.get(fmt, GenericSSEParser)

            if fmt != "generic":
                result = parser_cls.parse(raw_sse)
                if result is not None:
                    return result
                logger.warning(
                    "%s parser returned None, falling back to generic",
                    fmt,
                )

            # Always try generic as ultimate fallback
            return GenericSSEParser.parse(raw_sse)
        except (json.JSONDecodeError, Exception) as e:
            logger.debug(
                "SSE dispatch failed for line, trying next: %s",
                str(e)[:120],
            )
            continue

    return None
