"""
Stress tests for LLM Proxy management API.
Tests concurrent access patterns to verify the system handles high load.
"""
import asyncio
import time
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


# ---- Helper ----

async def make_client():
    """Create an async test client."""
    from main import app
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def login_admin() -> tuple[AsyncClient, str]:
    """Login as admin and return client + token."""
    ac = await make_client()
    resp = await ac.post("/api/auth/login", json={
        "username": "admin", "password": "admin123"
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return ac, token


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def report(name: str, total: int, results: dict):
    """Print test report."""
    avg_time = sum(results["times"]) / len(results["times"]) if results["times"] else 0
    p95 = sorted(results["times"])[int(len(results["times"]) * 0.95)] if results["times"] else 0
    p99 = sorted(results["times"])[int(len(results["times"]) * 0.99)] if results["times"] else 0
    print(f"\n  {name}: {total} concurrent")
    print(f"    Success: {results['success']} | Fail: {results['fail']}")
    print(f"    Avg: {avg_time*1000:.1f}ms | P95: {p95*1000:.1f}ms | P99: {p99*1000:.1f}ms")


# ---- Stress test: concurrent health checks ----

@pytest.mark.asyncio
async def test_stress_health_check(setup_database):
    """100 concurrent health check requests."""
    results = {"success": 0, "fail": 0, "times": []}

    async def hit_health(i):
        ac = await make_client()
        async with ac:
            start = time.time()
            resp = await ac.get("/api/health")
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [hit_health(i) for i in range(100)]
    await asyncio.gather(*tasks)

    report("Health Check", 100, results)
    assert results["fail"] == 0, f"{results['fail']} requests failed"


# ---- Stress test: concurrent logins ----

@pytest.mark.asyncio
async def test_stress_concurrent_login(setup_database):
    """50 concurrent login requests."""
    results = {"success": 0, "fail": 0, "times": []}

    async def do_login(i):
        ac = await make_client()
        async with ac:
            start = time.time()
            resp = await ac.post("/api/auth/login", json={
                "username": "admin", "password": "admin123"
            })
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [do_login(i) for i in range(50)]
    await asyncio.gather(*tasks)

    report("Concurrent Login", 50, results)
    assert results["fail"] == 0, f"{results['fail']} logins failed"


# ---- Stress test: concurrent port listing ----

@pytest.mark.asyncio
async def test_stress_concurrent_port_list(setup_database):
    """50 concurrent port listing requests."""
    ac, token = await login_admin()
    h = headers(token)
    results = {"success": 0, "fail": 0, "times": []}

    async def list_ports(i):
        client = await make_client()
        async with client:
            start = time.time()
            resp = await client.get("/api/ports", headers=h)
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [list_ports(i) for i in range(50)]
    await asyncio.gather(*tasks)
    await ac.aclose()

    report("Concurrent Port List", 50, results)
    assert results["fail"] == 0, f"{results['fail']} requests failed"


# ---- Stress test: concurrent user registrations ----

@pytest.mark.asyncio
async def test_stress_concurrent_register(setup_database):
    """50 concurrent user registrations."""
    results = {"success": 0, "fail": 0, "times": []}

    async def register_user(i):
        ac = await make_client()
        async with ac:
            start = time.time()
            resp = await ac.post("/api/auth/register", json={
                "username": f"stress_user_{i}_{int(time.time())}",
                "password": "testpass123"
            })
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [register_user(i) for i in range(50)]
    await asyncio.gather(*tasks)

    report("Concurrent Register", 50, results)
    assert results["fail"] == 0, f"{results['fail']} registrations failed"


# ---- Stress test: concurrent port creation ----

@pytest.mark.asyncio
async def test_stress_concurrent_port_create(setup_database):
    """30 concurrent port creation requests."""
    ac, token = await login_admin()
    h = headers(token)
    results = {"success": 0, "fail": 0, "times": []}

    async def create_port(i):
        client = await make_client()
        async with client:
            start = time.time()
            resp = await client.post("/api/ports", headers=h, json={
                "target_url": "https://httpbin.org",
                "description": f"Stress test port {i}"
            })
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [create_port(i) for i in range(30)]
    await asyncio.gather(*tasks)
    await ac.aclose()

    report("Concurrent Port Create", 30, results)
    assert results["fail"] == 0, f"{results['fail']} port creations failed"


# ---- Stress test: concurrent get_me ----

@pytest.mark.asyncio
async def test_stress_concurrent_get_me(setup_database):
    """100 concurrent /api/auth/me requests."""
    ac, token = await login_admin()
    h = headers(token)
    results = {"success": 0, "fail": 0, "times": []}

    async def get_me(i):
        client = await make_client()
        async with client:
            start = time.time()
            resp = await client.get("/api/auth/me", headers=h)
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [get_me(i) for i in range(50)]
    await asyncio.gather(*tasks)
    await ac.aclose()

    report("Concurrent Get Me", 50, results)
    assert results["fail"] == 0, f"{results['fail']} requests failed"


# ---- Stress test: concurrent admin list users ----

@pytest.mark.asyncio
async def test_stress_concurrent_admin_list(setup_database):
    """50 concurrent admin user list requests."""
    ac, token = await login_admin()
    h = headers(token)
    results = {"success": 0, "fail": 0, "times": []}

    async def list_users(i):
        client = await make_client()
        async with client:
            start = time.time()
            resp = await client.get("/api/admin/users", headers=h)
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [list_users(i) for i in range(50)]
    await asyncio.gather(*tasks)
    await ac.aclose()

    report("Concurrent Admin List", 50, results)
    assert results["fail"] == 0, f"{results['fail']} requests failed"


# ---- Stress test: concurrent config fetch ----

@pytest.mark.asyncio
async def test_stress_concurrent_config(setup_database):
    """100 concurrent config fetch requests."""
    results = {"success": 0, "fail": 0, "times": []}

    async def get_config(i):
        ac = await make_client()
        async with ac:
            start = time.time()
            resp = await ac.get("/api/config")
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [get_config(i) for i in range(100)]
    await asyncio.gather(*tasks)

    report("Concurrent Config Fetch", 100, results)
    assert results["fail"] == 0, f"{results['fail']} requests failed"


# ---- Stress test: mixed concurrent operations ----

@pytest.mark.asyncio
async def test_stress_mixed_operations(setup_database):
    """50 concurrent mixed operations (health, login, list, config)."""
    results = {"success": 0, "fail": 0, "times": []}

    async def mixed_op(i):
        ac = await make_client()
        async with ac:
            start = time.time()
            if i % 4 == 0:
                resp = await ac.get("/api/health")
            elif i % 4 == 1:
                resp = await ac.post("/api/auth/login", json={
                    "username": "admin", "password": "admin123"
                })
            elif i % 4 == 2:
                resp = await ac.get("/api/config")
            else:
                # Login first, then list ports
                login_resp = await ac.post("/api/auth/login", json={
                    "username": "admin", "password": "admin123"
                })
                if login_resp.status_code == 200:
                    token = login_resp.json()["access_token"]
                    resp = await ac.get("/api/ports", headers=headers(token))
                else:
                    resp = login_resp
            elapsed = time.time() - start
            results["times"].append(elapsed)
            if resp.status_code == 200:
                results["success"] += 1
            else:
                results["fail"] += 1

    tasks = [mixed_op(i) for i in range(80)]
    await asyncio.gather(*tasks)

    report("Mixed Operations", 80, results)
    assert results["fail"] == 0, f"{results['fail']} requests failed"
