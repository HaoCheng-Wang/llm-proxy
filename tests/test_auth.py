"""
Tests for authentication endpoints:
- POST /api/auth/register
- POST /api/auth/login
- GET  /api/auth/me
- POST /api/auth/change-password
"""
import pytest
import pytest_asyncio


@pytest.mark.asyncio
async def test_register_success(client):
    """New user can register successfully."""
    resp = await client.post("/api/auth/register", json={
        "username": "newuser_reg",
        "password": "password123"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "message" in data
    # Works with both REQUIRE_APPROVAL=true and false
    msg = data["message"].lower()
    assert any(kw in msg for kw in ("successful", "submitted", "approval")), f"Unexpected message: {data['message']}"


@pytest.mark.asyncio
async def test_register_duplicate(client):
    """Registering with existing username returns 400."""
    # Register once
    await client.post("/api/auth/register", json={
        "username": "dupuser",
        "password": "password123"
    })
    # Try again
    resp = await client.post("/api/auth/register", json={
        "username": "dupuser",
        "password": "password123"
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_register_short_password(client):
    """Registration with too-short password fails validation."""
    resp = await client.post("/api/auth/register", json={
        "username": "shortpass",
        "password": "ab"  # min_length=4
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_login_success(client, approved_user):
    """Approved user can login and receives token."""
    resp = await client.post("/api/auth/login", json={
        "username": "testuser",
        "password": "testpass123"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["username"] == "testuser"
    assert data["role"] == "user"


@pytest.mark.asyncio
async def test_login_wrong_password(client, approved_user):
    """Login with wrong password returns 401."""
    resp = await client.post("/api/auth/login", json={
        "username": "testuser",
        "password": "wrongpassword"
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_nonexistent_user(client):
    """Login with non-existent user returns 401."""
    resp = await client.post("/api/auth/login", json={
        "username": "ghostuser",
        "password": "whatever"
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_unapproved_user(client, unapproved_user):
    """Unapproved user cannot login (returns 403)."""
    resp = await client.post("/api/auth/login", json={
        "username": "pendinguser",
        "password": "pendingpass"
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_me(client, approved_user, user_headers):
    """GET /api/auth/me returns current user info."""
    resp = await client.get("/api/auth/me", headers=user_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "testuser"
    assert data["role"] == "user"


@pytest.mark.asyncio
async def test_get_me_no_token(client):
    """GET /api/auth/me without token returns 403."""
    resp = await client.get("/api/auth/me")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_me_invalid_token(client):
    """GET /api/auth/me with invalid token returns 401."""
    resp = await client.get("/api/auth/me", headers={
        "Authorization": "Bearer invalid.token.here"
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_change_password_success(client, approved_user, user_headers):
    """User can change their password."""
    resp = await client.post("/api/auth/change-password", headers=user_headers, json={
        "old_password": "testpass123",
        "new_password": "newpass456"
    })
    assert resp.status_code == 200
    assert "success" in resp.json()["message"].lower()


@pytest.mark.asyncio
async def test_change_password_wrong_old(client, approved_user, user_headers):
    """Change password with wrong old password fails."""
    resp = await client.post("/api/auth/change-password", headers=user_headers, json={
        "old_password": "wrongold",
        "new_password": "newpass456"
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_change_password_same_as_old(client, approved_user, user_headers):
    """Change password to same value fails."""
    resp = await client.post("/api/auth/change-password", headers=user_headers, json={
        "old_password": "testpass123",
        "new_password": "testpass123"
    })
    assert resp.status_code == 400
