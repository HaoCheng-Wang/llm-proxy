"""
Tests for admin endpoints:
- GET    /api/admin/users
- PUT    /api/admin/users/approve
- DELETE /api/admin/users/{user_id}
"""
import pytest
import pytest_asyncio


@pytest.mark.asyncio
async def test_admin_list_users(client, admin_headers, approved_user):
    """Admin can list all users."""
    resp = await client.get("/api/admin/users", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "users" in data
    assert len(data["users"]) >= 1


@pytest.mark.asyncio
async def test_admin_list_users_non_admin(client, user_headers):
    """Non-admin cannot list users."""
    resp = await client.get("/api/admin/users", headers=user_headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_approve_user(client, admin_headers, unapproved_user):
    """Admin can approve a pending user."""
    # Get user ID from list
    list_resp = await client.get("/api/admin/users", headers=admin_headers)
    users = list_resp.json()["users"]
    pending = [u for u in users if u["username"] == "pendinguser"]
    assert len(pending) == 1
    user_id = pending[0]["id"]

    resp = await client.put("/api/admin/users/approve", headers=admin_headers, json={
        "user_id": user_id,
        "is_approved": True
    })
    assert resp.status_code == 200
    assert "approved" in resp.json()["message"].lower()


@pytest.mark.asyncio
async def test_admin_reject_user(client, admin_headers):
    """Admin can reject a user."""
    # Register a new user to reject
    await client.post("/api/auth/register", json={
        "username": "rejectme",
        "password": "password123"
    })
    list_resp = await client.get("/api/admin/users", headers=admin_headers)
    users = list_resp.json()["users"]
    target = [u for u in users if u["username"] == "rejectme"]
    assert len(target) == 1

    resp = await client.put("/api/admin/users/approve", headers=admin_headers, json={
        "user_id": target[0]["id"],
        "is_approved": False
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_delete_user(client, admin_headers):
    """Admin can delete a non-admin user."""
    # Register a user to delete
    await client.post("/api/auth/register", json={
        "username": "deleteme",
        "password": "password123"
    })
    list_resp = await client.get("/api/admin/users", headers=admin_headers)
    users = list_resp.json()["users"]
    target = [u for u in users if u["username"] == "deleteme"]
    assert len(target) == 1

    resp = await client.delete(f"/api/admin/users/{target[0]['id']}", headers=admin_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_cannot_delete_admin(client, admin_headers):
    """Admin cannot delete another admin."""
    # Try to delete admin (id=1)
    resp = await client.delete("/api/admin/users/1", headers=admin_headers)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_admin_delete_nonexistent(client, admin_headers):
    """Deleting non-existent user returns 404."""
    resp = await client.delete("/api/admin/users/99999", headers=admin_headers)
    assert resp.status_code == 404
