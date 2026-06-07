"""
Integration + Stress tests for LLM Proxy.

Non-DB integration: direct router function testing (no TestClient lifecycle).
Stress: concurrent SSE parsing, deep_merge, _sanitize_text, large inputs.
"""
import sys
import os
import json
import time
import datetime
import asyncio
import concurrent.futures

ROOT = r"D:\homework\llm-proxy"
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import pytest


# ═══════════════════════════════════════════════════════════════════
#  Non-DB endpoint logic tests (test router functions directly)
# ═══════════════════════════════════════════════════════════════════

class TestEndpointLogic:
    """Test the actual functions backing the API endpoints, with mocked deps."""

    def test_register_logic_ok(self):
        """register() with no existing user."""
        from routers.auth_router import register
        from schemas import UserRegister
        import models

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return None
            def add(self, obj): self._added = obj
            def commit(self): pass

        db = FakeDB()
        result = register(UserRegister(username="alice", password="12345678"), db=db)
        assert "registration submitted" in result["message"].lower()

    def test_register_logic_duplicate(self):
        from routers.auth_router import register
        from schemas import UserRegister
        from fastapi import HTTPException
        from models import User
        from auth import hash_password

        existing = User(username="alice", password_hash=hash_password("x"),
                        role="user", is_approved=True)

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return existing

        db = FakeDB()
        with pytest.raises(HTTPException) as exc:
            register(UserRegister(username="alice", password="12345678"), db=db)
        assert exc.value.status_code == 400

    def test_login_logic_ok(self):
        from routers.auth_router import login
        from schemas import UserLogin
        from models import User
        from auth import hash_password

        user = User(id=42, username="bob", password_hash=hash_password("pw"),
                    role="user", is_approved=True)

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return user

        result = login(UserLogin(username="bob", password="pw"), db=FakeDB())
        assert result.username == "bob"
        assert result.role == "user"

    def test_login_logic_bad_pw(self):
        from routers.auth_router import login
        from schemas import UserLogin
        from models import User
        from auth import hash_password
        from fastapi import HTTPException

        user = User(username="bob", password_hash=hash_password("real"),
                    role="user", is_approved=True)

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return user

        with pytest.raises(HTTPException) as exc:
            login(UserLogin(username="bob", password="wrong"), db=FakeDB())
        assert exc.value.status_code == 401

    def test_login_logic_unapproved(self):
        from routers.auth_router import login
        from schemas import UserLogin
        from models import User
        from auth import hash_password
        from fastapi import HTTPException

        user = User(username="bob", password_hash=hash_password("pw"),
                    role="user", is_approved=False)

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return user

        with pytest.raises(HTTPException) as exc:
            login(UserLogin(username="bob", password="pw"), db=FakeDB())
        assert exc.value.status_code == 403

    def test_get_me(self):
        from routers.auth_router import get_me
        from models import User
        user = User(username="bob", role="user", is_approved=True)
        result = get_me(current_user=user)
        assert result.username == "bob"

    def test_change_password_logic(self):
        from routers.auth_router import change_password
        from schemas import ChangePasswordRequest
        from models import User
        from auth import hash_password

        user = User(username="bob", password_hash=hash_password("old"),
                    role="user", is_approved=True)

        class FakeDB:
            def commit(self): pass

        result = change_password(
            ChangePasswordRequest(old_password="old", new_password="new123"),
            current_user=user, db=FakeDB()
        )
        assert "success" in result["message"].lower()

    def test_admin_list_users(self):
        from routers.admin_router import list_users
        from models import User
        admin = User(id=1, username="admin", role="admin", is_approved=True,
                     created_at=datetime.datetime(2025, 1, 1))
        users = [admin, User(id=2, username="bob", role="user", is_approved=True,
                             created_at=datetime.datetime(2025, 1, 1))]

        class FakeDB:
            def query(self, model): return self
            def order_by(self, *a): return self
            def all(self): return users

        result = list_users(admin=admin, db=FakeDB())
        assert len(result.users) == 2

    def test_admin_approve_user(self):
        from routers.admin_router import approve_user
        from schemas import UserApproval
        from models import User

        target = User(id=3, username="newbie", role="user", is_approved=False)
        admin = User(username="admin", role="admin")

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return target
            def commit(self): pass

        result = approve_user(
            UserApproval(user_id=3, is_approved=True), admin=admin, db=FakeDB()
        )
        assert target.is_approved is True

    def test_admin_cannot_modify_admin(self):
        from routers.admin_router import approve_user
        from schemas import UserApproval
        from models import User
        from fastapi import HTTPException

        target = User(id=1, username="superadmin", role="admin")
        admin = User(username="admin", role="admin")

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return target

        with pytest.raises(HTTPException) as exc:
            approve_user(
                UserApproval(user_id=1, is_approved=False), admin=admin, db=FakeDB()
            )
        assert exc.value.status_code == 400

    def test_admin_delete_user(self):
        from routers.admin_router import delete_user
        from models import User

        target = User(id=5, username="gone", role="user")
        admin = User(username="admin", role="admin")

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return target
            def delete(self, obj): pass
            def commit(self): pass

        result = delete_user(5, admin=admin, db=FakeDB())
        assert "deleted" in result["message"].lower()

    def test_port_history_logic(self):
        """get_port_history with valid port."""
        from routers.ports_router import get_port_history
        from models import Port, Request as RModel, User
        import datetime as dt

        port = Port(id=1, port_number=12345, user_id=2, target_url="https://x.com",
                    is_active=True, created_at=dt.datetime(2025, 1, 1))
        req = RModel(id=1, port_id=1, method="POST", path="/v1/chat",
                     status_code=200, created_at=dt.datetime(2025, 1, 1),
                     reconstruction_error=False)
        user = User(id=2, username="bob", role="user", is_approved=True,
                    created_at=dt.datetime(2025, 1, 1))
        current = User(id=2, username="bob", role="user", is_approved=True)

        class FakeDB:
            def query(self, model):
                f = FakeDB()
                f._model = model
                return f
            def filter(self, *a): return self
            def order_by(self, *a): return self
            def offset(self, n): return self
            def limit(self, n): return self
            def first(self):
                if getattr(self, '_model', None) == User:
                    return user
                if getattr(self, '_model', None) in (Port, None):
                    return port
                return req
            def all(self):
                if getattr(self, '_model', None) == RModel:
                    return [req]
                return []
            def scalar(self): return None
            def count(self, *a):
                class C:
                    def scalar(self_): return 1
                return C()

        result = get_port_history(1, current_user=current, db=FakeDB())
        assert result.port.port_number == 12345

    def test_port_not_found(self):
        from routers.ports_router import get_port_history
        from models import User
        from fastapi import HTTPException

        current = User(id=2, username="bob", role="user")

        class FakeDB:
            def query(self, model): return self
            def filter(self, *a): return self
            def first(self): return None

        with pytest.raises(HTTPException) as exc:
            get_port_history(99999, current_user=current, db=FakeDB())
        assert exc.value.status_code == 404

    def test_config_endpoint(self):
        from routers.config_router import get_config
        result = get_config()
        assert "api_port" in result
        assert "display_ip" in result


# ═══════════════════════════════════════════════════════════════════
#  Stress / Concurrency tests
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.stress
class TestStressSSEParsers:
    def _gen_anthropic_stream(self, text_len: int) -> str:
        lines = [
            'data: {"type":"message_start","message":{"id":"s","model":"c","role":"assistant"}}'
        ]
        text = ""
        words = ["abc ", "def ", "ghi ", "jkl ", "mno ", "pqr ", "stu ", "vwx ", "yz ", ". "]
        while len(text) < text_len:
            for w in words:
                text += w
                if len(text) >= text_len:
                    break
        lines.append(
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
        )
        for i in range(0, len(text), 50):
            chunk = json.dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text[i:i+50]}
            }, ensure_ascii=False)
            lines.append(f"data: {chunk}")
        lines.append('data: {"type":"content_block_stop","index":0}')
        lines.append(
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":999}}'
        )
        lines.append('data: {"type":"message_stop"}')
        return "\n".join(lines) + "\n"

    @pytest.mark.slow
    def test_parse_1mb_anthropic_stream(self):
        from proxy_app import _parse_anthropic_sse
        sse = self._gen_anthropic_stream(1_000_000)
        t0 = time.perf_counter()
        result = _parse_anthropic_sse(sse)
        elapsed = time.perf_counter() - t0
        assert result is not None
        obj = json.loads(result)
        assert len(obj["content"][0]["text"]) > 900_000
        assert elapsed < 3.0, f"{elapsed:.2f}s"

    def test_parse_100_small_streams_threadpool(self):
        from proxy_app import _parse_anthropic_sse

        def parse_one(i):
            sse = self._gen_anthropic_stream(1000)
            return _parse_anthropic_sse(sse) is not None

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(parse_one, range(100)))
        elapsed = time.perf_counter() - t0
        assert all(results)
        assert elapsed < 5.0, f"{elapsed:.2f}s"

    def test_parse_gemini_10k_chunks(self):
        from proxy_app import _parse_gemini_sse
        lines = []
        for i in range(10000):
            obj = {"candidates": [{"content": {"parts": [{"text": f"w{i} "}]}}]}
            lines.append(f"data: {json.dumps(obj, ensure_ascii=False)}")
        sse = "\n".join(lines) + "\n"
        t0 = time.perf_counter()
        result = _parse_gemini_sse(sse)
        elapsed = time.perf_counter() - t0
        assert result is not None
        assert elapsed < 1.5, f"{elapsed:.2f}s"

    def test_parse_all_6_formats_concurrently(self):
        """Spawn all 6 parser types concurrently — no interference."""
        from proxy_app import (
            _parse_anthropic_sse, _parse_openai_chat_sse,
            _parse_openai_responses_sse, _parse_gemini_sse,
            _reconstruct_sse_universal, _parse_generic_sse,
        )

        anthropic_sse = self._gen_anthropic_stream(5000)
        chat_sse = '\n'.join([
            'data: {"id":"c1","object":"chat.completion.chunk","model":"gpt","choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"delta":{"content":" World"}}]}',
            'data: [DONE]'
        ]) + '\n'
        gemini_sse = '\n'.join([
            'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}',
            'data: {"candidates":[{"content":{"parts":[{"text":" World"}]}}]}',
        ]) + '\n'
        response_sse = '\n'.join([
            'data: {"type":"response.created","response":{"id":"r1","model":"gpt"}}',
            'data: {"type":"response.output_text.delta","delta":"Hi"}',
            'data: {"type":"response.completed","response":{}}',
        ]) + '\n'

        tasks = [
            (_parse_anthropic_sse, anthropic_sse),
            (_parse_openai_chat_sse, chat_sse),
            (_parse_gemini_sse, gemini_sse),
            (_parse_openai_responses_sse, response_sse),
            (_reconstruct_sse_universal, chat_sse),
            (_parse_generic_sse, chat_sse),
        ]

        def run_one(fn, sse):
            return fn(sse) is not None

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futures = [ex.submit(run_one, fn, sse) for fn, sse in tasks]
            results = [f.result() for f in futures]
        assert all(results), f"Some parsers failed: {results}"


@pytest.mark.stress
class TestStressSanitize:
    def test_sanitize_5mb_with_surrogates(self):
        from proxy_app import _sanitize_text
        parts = []
        for i in range(5000):
            parts.append("x" * 500)
            if i % 2 == 0:
                parts.append(chr(0xD800 + (i % 1024)))
        value = "".join(parts)
        t0 = time.perf_counter()
        _sanitize_text(value)
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"{elapsed:.2f}s"


@pytest.mark.stress
class TestStressDeepMerge:
    def test_merge_1000_chunks(self):
        from proxy_app import _deep_merge
        merged = {}
        for i in range(1000):
            merged = _deep_merge(merged, {"text": f"c{i} "})
        assert merged["text"].startswith("c0")
        assert merged["text"].endswith("c999 ")

    def test_merge_deeply_nested(self):
        from proxy_app import _deep_merge
        base = {}
        for i in range(11):
            base = _deep_merge(base, {
                "level": {"nested": {"deep": {"value": f"d{i}"}}},
            })
        # Same-path strings concatenate
        assert base["level"]["nested"]["deep"]["value"].startswith("d0")

        # For list test: different keys merge independently
        base2 = {}
        for i in range(11):
            base2 = _deep_merge(base2, {f"key{i}": f"val{i}"})
        assert len(base2) == 11


@pytest.mark.stress
class TestStressConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_port_cache_access(self):
        import proxy_app
        proxy_app._port_target_cache = {i: f"https://api{i}.com" for i in range(100)}
        proxy_app._cache_updated_at = time.time()

        async def lookup(i):
            return proxy_app._port_target_cache.get(i % 100) is not None

        tasks = [lookup(i) for i in range(500)]
        t0 = time.perf_counter()
        results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - t0
        assert all(results)
        assert elapsed < 0.5, f"{elapsed:.2f}s"
        proxy_app._port_target_cache.clear()

    @pytest.mark.asyncio
    async def test_many_concurrent_serialize_body(self):
        """50 concurrent _serialize_body calls — no crashes."""
        from proxy_app import _serialize_body
        body = json.dumps({"key": "value", "list": [1, 2, 3]}).encode()

        async def ser_one():
            return _serialize_body(body)

        tasks = [ser_one() for _ in range(50)]
        results = await asyncio.gather(*tasks)
        assert all(r is not None for r in results)
