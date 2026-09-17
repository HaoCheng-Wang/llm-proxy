"""Tests for the streaming-save path: body materialisation and buffer handling.

Covers proxy_app._finalize_stream_body, which reads the buffered SSE spool and
decides what to store, and _save_to_db's ownership of that buffer.
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import proxy_app
import database
import sse_parsers
from config import PROXY_BODY_MEMORY_LIMIT


def chat_chunk(text, finish=None, with_usage=False):
    obj = {
        "id": "chatcmpl-t", "object": "chat.completion.chunk", "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": ({"content": text} if text else {}),
                     "finish_reason": finish}],
    }
    if with_usage:
        obj["usage"] = {"prompt_tokens": 1, "completion_tokens": 2,
                        "total_tokens": 3}
    return "data: " + json.dumps(obj) + "\n\n"


GOOD_SSE = (
    chat_chunk("Hello")
    + chat_chunk(" world")
    + chat_chunk("", finish="stop", with_usage=True)
    + "data: [DONE]\n\n"
)


def anth(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
                   for e in events)


class TestAnthropicBlockReuse:
    """A relay that re-emits content_block_start for a used index.

    _fragments is keyed by (index, field) and outlives the block dict that
    content_block_start replaces, so fragments from a previous incarnation
    must be discarded with it — otherwise they are joined into the new block
    and duplicate text the pre-refactor parser dropped.
    """

    @staticmethod
    def _text_of(sse):
        return json.loads(sse_parsers.reconstruct_sse_to_json(sse))["content"][0]["text"]

    def test_repeated_start_discards_earlier_fragments(self):
        sse = anth(
            {"type": "message_start", "message": {"id": "m", "role": "assistant"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "FIRST"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "SECOND"}},
        )
        assert self._text_of(sse) == "SECOND"

    def test_repeated_start_without_stop(self):
        sse = anth(
            {"type": "message_start", "message": {"id": "m", "role": "assistant"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "A"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "B"}},
        )
        assert self._text_of(sse) == "B"

    def test_restart_does_not_touch_other_indices(self):
        sse = anth(
            {"type": "message_start", "message": {"id": "m", "role": "assistant"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "keep"}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "second"}},
        )
        blocks = json.loads(
            sse_parsers.reconstruct_sse_to_json(sse))["content"]
        assert [b["text"] for b in blocks] == ["keep", "second"]

    def test_type_change_on_same_index(self):
        sse = anth(
            {"type": "message_start", "message": {"id": "m", "role": "assistant"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "text part"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "think part"}},
        )
        block = json.loads(
            sse_parsers.reconstruct_sse_to_json(sse))["content"][0]
        assert block["type"] == "thinking"
        assert block["thinking"] == "think part"


def spool(data: bytes):
    """A SpooledTemporaryFile in the same state the proxy hands over."""
    buf = tempfile.SpooledTemporaryFile(max_size=PROXY_BODY_MEMORY_LIMIT)
    buf.write(data)
    return buf


class TestFinalizeStreamBody:
    def test_reconstructs_valid_stream(self):
        body, raw, err = proxy_app._finalize_stream_body(
            spool(GOOD_SSE.encode()), 12345)
        assert err is False
        assert raw is None, "raw SSE must not be duplicated on success"
        parsed = json.loads(body)
        assert parsed["choices"][0]["message"]["content"] == "Hello world"
        assert parsed["usage"]["total_tokens"] == 3

    def test_empty_stream(self):
        body, raw, err = proxy_app._finalize_stream_body(spool(b""), 1)
        assert (body, raw, err) == (None, None, False)

    def test_unparseable_stream_keeps_raw_for_inspection(self):
        """Reconstruction failure keeps raw — the UI links to it."""
        data = b"this is not sse\n"
        body, raw, err = proxy_app._finalize_stream_body(spool(data), 1)
        assert err is True
        assert raw is not None
        assert body == raw

    def test_oversized_stream_stores_body_only(self, monkeypatch):
        """Over the limit: truncated text is the *only* copy kept.

        Keeping it in response_body_raw as well doubled the row to ~2x the
        limit, past MySQL's max_allowed_packet, so the INSERT failed and the
        whole record was lost.
        """
        monkeypatch.setattr(proxy_app, "SSE_RECONSTRUCT_MAX_BYTES", 200)
        data = (GOOD_SSE * 50).encode()
        assert len(data) > 200
        body, raw, err = proxy_app._finalize_stream_body(spool(data), 1)

        assert err is True
        assert raw is None, "truncated text must not be stored twice"
        assert "[TRUNCATED:" in body
        assert len(body.encode()) < len(data)
        # Row size stays under the packet limit that a duplicate would breach.
        assert len(body.encode()) <= 200 + 100

    def test_oversized_row_fits_max_allowed_packet(self, monkeypatch):
        """The real limit: a duplicated copy would exceed max_allowed_packet."""
        max_allowed_packet = 64 * 1024 * 1024
        monkeypatch.setattr(proxy_app, "SSE_RECONSTRUCT_MAX_BYTES",
                            max_allowed_packet - 1024)
        data = b"x" * (max_allowed_packet + 4096)
        buf = tempfile.SpooledTemporaryFile(max_size=1024)  # force disk spool
        buf.write(data)
        body, raw, err = proxy_app._finalize_stream_body(buf, 1)
        assert err is True
        row = len(body.encode()) + (len(raw.encode()) if raw else 0)
        assert row < max_allowed_packet, (
            f"row {row} would exceed max_allowed_packet {max_allowed_packet}")


class TestSaveToDbBufferOwnership:
    async def test_buffer_is_closed_even_when_write_fails(
            self, setup_database, monkeypatch):
        """The buffer must not leak when every write attempt fails."""
        buf = spool(GOOD_SSE.encode())

        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(proxy_app, "RequestModel", boom)
        monkeypatch.setattr(proxy_app.time, "sleep", lambda *_: None)
        proxy_app._save_to_db(
            999999, "POST", "/v1/chat/completions", "{}", None, "{}", None,
            200, 5, stream_buf=buf,
        )
        assert buf.closed, "stream buffer leaked after a failed save"

    async def test_materialization_leaves_the_event_loop(
            self, setup_database, monkeypatch):
        """The O(body) decode+reconstruct must not run on the event loop.

        Asserted by thread identity: if it ran inline, a large stream would
        stall every other in-flight request for the duration.
        """
        import threading

        buf = spool(GOOD_SSE.encode())
        loop_thread = threading.get_ident()
        seen = {}

        real_finalize = proxy_app._finalize_stream_body

        def record_thread(*a, **k):
            seen["thread"] = threading.get_ident()
            return real_finalize(*a, **k)

        monkeypatch.setattr(proxy_app, "_finalize_stream_body", record_thread)

        await proxy_app._save_record_async(
            999999, "POST", "/v1/chat/completions", "{}", None, "{}", None,
            200, 5, stream_buf=buf,
        )

        assert "thread" in seen, "materialisation never ran"
        assert seen["thread"] != loop_thread, (
            "decode+reconstruction ran on the event loop"
        )
        assert buf.closed

    async def test_buffer_materialized_once_across_retries(
            self, setup_database, monkeypatch):
        """Retries must not re-read the buffer — it is consumed once, up front.

        Reading it inside the retry loop would fail on the second attempt,
        because a consumed SpooledTemporaryFile cannot be replayed.
        """
        buf = spool(GOOD_SSE.encode())
        materialized = {"n": 0}
        real_finalize = proxy_app._finalize_stream_body

        def counting(*a, **k):
            materialized["n"] += 1
            return real_finalize(*a, **k)

        monkeypatch.setattr(proxy_app, "_finalize_stream_body", counting)

        # Fail the first record construction so the loop retries once.
        real_model = proxy_app.RequestModel
        attempts = {"n": 0}

        def flaky_model(*a, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient")
            return real_model(*a, **k)

        monkeypatch.setattr(proxy_app, "RequestModel", flaky_model)
        monkeypatch.setattr(proxy_app.time, "sleep", lambda *_: None)

        proxy_app._save_to_db(
            999999, "POST", "/v1/chat/completions", "{}", None, "{}", None,
            200, 5, stream_buf=buf,
        )

        assert materialized["n"] == 1, "buffer must be read exactly once"
        assert attempts["n"] >= 2, "the retry path was not exercised"
        assert buf.closed
