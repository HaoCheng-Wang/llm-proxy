"""
Tests for port management endpoints:
- POST   /api/ports
- GET    /api/ports
- GET    /api/ports/{port_id}
- DELETE /api/ports/{port_id}
- POST   /api/ports/{port_id}/stop
- POST   /api/ports/{port_id}/start
- DELETE /api/ports/{port_id}/history
- GET    /api/ports/{port_id}/export
- GET    /api/ports/active-ports
"""
import pytest
import pytest_asyncio


@pytest.mark.asyncio
async def test_create_port(client, admin_headers):
    """Admin can create a proxy port."""
    resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://api.openai.com",
        "description": "Test port"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "port_number" in data
    assert data["target_url"] == "https://api.openai.com"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_create_port_invalid_url(client, admin_headers):
    """Creating port with invalid URL fails."""
    resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "ftp://invalid.com",
        "description": "Bad scheme"
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_ports(client, admin_headers):
    """Admin can list all ports."""
    resp = await client.get("/api/ports", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_port_history(client, admin_headers):
    """Get history for a port."""
    # Create a port first
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "History test port"
    })
    port_id = create_resp.json()["id"]

    resp = await client.get(f"/api/ports/{port_id}", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "port" in data
    assert "requests" in data


@pytest.mark.asyncio
async def test_get_port_history_nonexistent(client, admin_headers):
    """Get history for non-existent port returns 404."""
    resp = await client.get("/api/ports/99999", headers=admin_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stop_and_start_port(client, admin_headers):
    """Stop and then start a port."""
    # Create port
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Stop/start test"
    })
    port_id = create_resp.json()["id"]

    # Stop
    resp = await client.post(f"/api/ports/{port_id}/stop", headers=admin_headers)
    assert resp.status_code == 200

    # Start
    resp = await client.post(f"/api/ports/{port_id}/start", headers=admin_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_port(client, admin_headers):
    """Delete a port."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Delete test"
    })
    port_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/ports/{port_id}", headers=admin_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_port_nonexistent(client, admin_headers):
    """Delete non-existent port returns 404."""
    resp = await client.delete("/api/ports/99999", headers=admin_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_clear_port_history(client, admin_headers):
    """Clear history for a port."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Clear history test"
    })
    port_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/ports/{port_id}/history", headers=admin_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_export_port_history(client, admin_headers):
    """Export history for a port."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Export test"
    })
    port_id = create_resp.json()["id"]

    resp = await client.get(f"/api/ports/{port_id}/export", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "port" in data
    assert "requests" in data


@pytest.mark.asyncio
async def test_export_with_method_filter(client, admin_headers):
    """Export with method filter."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Export filter test"
    })
    port_id = create_resp.json()["id"]

    resp = await client.get(f"/api/ports/{port_id}/export?method_filter=api", headers=admin_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_active_ports(client, admin_headers):
    """Get active port numbers."""
    resp = await client.get("/api/ports/active-ports", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_port_access_denied_for_unapproved(client, unapproved_user):
    """Unapproved user cannot access port endpoints."""
    from auth import create_access_token
    # Get the user from DB to get their ID
    import database
    from models import User
    db = database.SessionLocal()
    try:
        user = db.query(User).filter(User.username == "pendinguser").first()
        if user:
            token = create_access_token({"user_id": user.id, "role": "user"})
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.get("/api/ports", headers=headers)
            assert resp.status_code == 403
    finally:
        db.close()
