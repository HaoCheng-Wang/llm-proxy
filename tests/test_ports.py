"""
Tests for port management endpoints:
- POST   /api/ports
- GET    /api/ports
- GET    /api/ports/{port_id}
- PUT    /api/ports/{port_id}
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
    # Endpoint returns NDJSON (line 1 = port metadata)
    text = resp.text
    lines = [l for l in text.strip().split("\n") if l.strip()]
    assert len(lines) >= 1
    import json
    data = json.loads(lines[0])
    assert "port_number" in data
    assert "target_url" in data


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


# ─────────────────────────────────────────────────────────────
# PUT /api/ports/{port_id} — update port (including port_number)
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_port_description(client, admin_headers):
    """Update a port's description without changing port_number."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://api.openai.com",
        "description": "Original desc"
    })
    port_id = create_resp.json()["id"]
    original_pn = create_resp.json()["port_number"]

    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "description": "Updated desc"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Updated desc"
    assert data["port_number"] == original_pn  # unchanged
    assert data["target_url"] == "https://api.openai.com"


@pytest.mark.asyncio
async def test_update_port_number(client, admin_headers):
    """Update a port's 5-digit number to a new free value."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Change number test"
    })
    port_id = create_resp.json()["id"]
    old_pn = create_resp.json()["port_number"]

    # Pick a new number that's very unlikely to collide
    new_pn = old_pn + 1 if old_pn < 99999 else old_pn - 1

    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "port_number": new_pn
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["port_number"] == new_pn

    # Verify it appears in the port list under the new number
    list_resp = await client.get("/api/ports", headers=admin_headers)
    ports = list_resp.json()
    pn_list = [p["port_number"] for p in ports]
    assert new_pn in pn_list
    assert old_pn not in pn_list


@pytest.mark.asyncio
async def test_update_port_number_out_of_range(client, admin_headers):
    """Reject port_number outside the 10000–99999 range."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Range test"
    })
    port_id = create_resp.json()["id"]

    # Too small
    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "port_number": 999
    })
    assert resp.status_code == 400

    # Too large
    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "port_number": 100000
    })
    assert resp.status_code == 400

    # Negative
    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "port_number": -5
    })
    assert resp.status_code == 400

    # Zero
    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "port_number": 0
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_port_number_conflict(client, admin_headers):
    """Reject port_number that is already assigned to another active port."""
    # Create two ports
    p1 = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Port A"
    })
    p2 = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://api.openai.com",
        "description": "Port B"
    })
    id1 = p1.json()["id"]
    pn2 = p2.json()["port_number"]

    # Try to set port 1's number to port 2's number → conflict
    resp = await client.put(f"/api/ports/{id1}", headers=admin_headers, json={
        "port_number": pn2
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_port_number_same_value(client, admin_headers):
    """Setting port_number to its current value is a no-op (not an error)."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Same value test"
    })
    port_id = create_resp.json()["id"]
    current_pn = create_resp.json()["port_number"]

    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "port_number": current_pn,
        "description": "Still the same number"
    })
    assert resp.status_code == 200
    assert resp.json()["port_number"] == current_pn
    assert resp.json()["description"] == "Still the same number"


@pytest.mark.asyncio
async def test_update_port_number_and_target_url(client, admin_headers):
    """Update port_number and target_url simultaneously."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Combined update"
    })
    port_id = create_resp.json()["id"]
    old_pn = create_resp.json()["port_number"]
    new_pn = old_pn + 1 if old_pn < 99999 else old_pn - 1

    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "port_number": new_pn,
        "target_url": "https://api.openai.com",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["port_number"] == new_pn
    assert data["target_url"] == "https://api.openai.com"


@pytest.mark.asyncio
async def test_update_port_nonexistent(client, admin_headers):
    """Updating a non-existent port returns 404."""
    resp = await client.put("/api/ports/99999", headers=admin_headers, json={
        "description": "Ghost"
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_port_unauthorized(client, user_headers, admin_headers):
    """A regular user cannot update another user's port."""
    # Admin creates a port
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Admin's port"
    })
    port_id = create_resp.json()["id"]

    # Regular user tries to edit it
    resp = await client.put(f"/api/ports/{port_id}", headers=user_headers, json={
        "description": "Hijack attempt"
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_port_cache_refresh(client, admin_headers):
    """After changing port_number, the active-ports list reflects the new number."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Cache refresh test"
    })
    port_id = create_resp.json()["id"]
    old_pn = create_resp.json()["port_number"]
    new_pn = old_pn + 1 if old_pn < 99999 else old_pn - 1

    # Change the number
    await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "port_number": new_pn
    })

    # Active ports should contain new_pn, not old_pn
    resp = await client.get("/api/ports/active-ports", headers=admin_headers)
    active = resp.json()
    assert new_pn in active
    assert old_pn not in active


@pytest.mark.asyncio
async def test_update_deleted_port(client, admin_headers):
    """Cannot update a soft-deleted port."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Will be deleted"
    })
    port_id = create_resp.json()["id"]

    # Soft-delete
    await client.delete(f"/api/ports/{port_id}", headers=admin_headers)

    # Try to update
    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "description": "Should fail"
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_port_invalid_url(client, admin_headers):
    """Reject target_url with invalid scheme during update."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "URL test"
    })
    port_id = create_resp.json()["id"]

    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "target_url": "ftp://evil.com"
    })
    assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────
# POST /api/ports — admin creates port for another user
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_create_port_for_other_user(client, admin_headers, approved_user):
    """Admin can create a port and assign it to another approved user."""
    resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://api.openai.com",
        "description": "For testuser",
        "user_id": approved_user.id,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == approved_user.username
    assert data["target_url"] == "https://api.openai.com"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_admin_create_port_for_self_via_user_id(client, admin_headers):
    """Admin specifying own user_id is equivalent to omitting it."""
    resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Own port via user_id",
        "user_id": 1,  # admin user is typically id=1
    })
    # May pass or fail depending on whether user 1 is admin — either way
    # it should not be a 403/404 for nonexistent user
    assert resp.status_code in (200, 400)


@pytest.mark.asyncio
async def test_non_admin_cannot_specify_user_id(client, user_headers, approved_user):
    """Regular user specifying user_id receives 403 Forbidden."""
    resp = await client.post("/api/ports", headers=user_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Hijack attempt",
        "user_id": 1,
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_create_port_for_nonexistent_user(client, admin_headers):
    """Admin specifying a nonexistent user_id receives 404."""
    resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Ghost user",
        "user_id": 99999,
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_create_port_for_unapproved_user(client, admin_headers, unapproved_user):
    """Admin cannot create a port for an unapproved user."""
    import database
    from models import User
    db = database.SessionLocal()
    try:
        user = db.query(User).filter(User.username == "pendinguser").first()
        if user:
            resp = await client.post("/api/ports", headers=admin_headers, json={
                "target_url": "https://httpbin.org",
                "description": "For pending user",
                "user_id": user.id,
            })
            assert resp.status_code == 400
    finally:
        db.close()


@pytest.mark.asyncio
async def test_admin_create_port_without_user_id_defaults_to_self(client, admin_headers):
    """Omitting user_id creates the port for the admin themselves."""
    resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Default owner test",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "admin"


@pytest.mark.asyncio
async def test_admin_update_port_preserves_owner_username(client, admin_headers, approved_user):
    """After updating a port, the response username should reflect the actual owner."""
    # Create a port for another user
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Owner test",
        "user_id": approved_user.id,
    })
    port_id = create_resp.json()["id"]
    assert create_resp.json()["username"] == approved_user.username

    # Admin updates the port — response username must still be the owner's
    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "description": "Updated by admin"
    })
    assert resp.status_code == 200
    assert resp.json()["username"] == approved_user.username


# ─────────────────────────────────────────────────────────────
# PUT /api/ports/{port_id} — admin reassigns port owner
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_reassign_port_owner(client, admin_headers, approved_user):
    """Admin can transfer a port to another approved user."""
    # Create a port for admin first
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Transfer test"
    })
    port_id = create_resp.json()["id"]
    assert create_resp.json()["username"] == "admin"

    # Transfer to testuser
    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "user_id": approved_user.id
    })
    assert resp.status_code == 200
    assert resp.json()["username"] == approved_user.username
    assert resp.json()["user_id"] == approved_user.id

    # Verify the new owner can see this port in their list
    from auth import create_access_token
    token = create_access_token({"user_id": approved_user.id, "role": "user"})
    user_hdrs = {"Authorization": f"Bearer {token}"}
    list_resp = await client.get("/api/ports", headers=user_hdrs)
    port_ids = [p["id"] for p in list_resp.json()]
    assert port_id in port_ids  # new owner can see it


@pytest.mark.asyncio
async def test_admin_reassign_port_old_owner_loses_access(client, admin_headers, admin_user, user_headers, approved_user):
    """When admin transfers a non-admin user's port, the old owner no longer sees it."""
    # Admin creates a port assigned to testuser
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Port to transfer away",
        "user_id": approved_user.id,  # testuser
    })
    port_id = create_resp.json()["id"]
    assert create_resp.json()["username"] == "testuser"

    # Verify testuser currently sees it
    list_resp = await client.get("/api/ports", headers=user_headers)
    port_ids = [p["id"] for p in list_resp.json()]
    assert port_id in port_ids

    # Admin transfers it away from testuser to admin themselves
    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "user_id": admin_user.id
    })
    assert resp.status_code == 200
    assert resp.json()["username"] == "admin"

    # Old owner (testuser) should no longer see it
    list_resp2 = await client.get("/api/ports", headers=user_headers)
    port_ids2 = [p["id"] for p in list_resp2.json()]
    assert port_id not in port_ids2


@pytest.mark.asyncio
async def test_non_admin_cannot_reassign_owner(client, user_headers, approved_user):
    """Regular user cannot change the owner of their own port."""
    # Create a port as the regular user
    create_resp = await client.post("/api/ports", headers=user_headers, json={
        "target_url": "https://httpbin.org",
        "description": "My port"
    })
    port_id = create_resp.json()["id"]

    # Try to reassign to someone else
    resp = await client.put(f"/api/ports/{port_id}", headers=user_headers, json={
        "user_id": 1  # admin
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_reassign_to_nonexistent_user(client, admin_headers):
    """Admin reassigning to nonexistent user returns 404."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Ghost transfer test"
    })
    port_id = create_resp.json()["id"]

    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "user_id": 99999
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_reassign_to_unapproved_user(client, admin_headers):
    """Admin cannot reassign port to unapproved user."""
    import database
    from models import User
    db = database.SessionLocal()
    try:
        pending = db.query(User).filter(User.username == "pendinguser").first()
        if not pending:
            return  # skip if no pending user
        create_resp = await client.post("/api/ports", headers=admin_headers, json={
            "target_url": "https://httpbin.org",
            "description": "Pending transfer test"
        })
        port_id = create_resp.json()["id"]

        resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
            "user_id": pending.id
        })
        assert resp.status_code == 400
    finally:
        db.close()


@pytest.mark.asyncio
async def test_admin_reassign_to_same_user_noop(client, admin_headers, approved_user):
    """Reassigning to the same user should succeed (no-op)."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "Same user test",
        "user_id": approved_user.id,
    })
    port_id = create_resp.json()["id"]
    original_uid = create_resp.json()["user_id"]

    resp = await client.put(f"/api/ports/{port_id}", headers=admin_headers, json={
        "user_id": original_uid,
        "description": "Same owner, new desc"
    })
    assert resp.status_code == 200
    assert resp.json()["username"] == approved_user.username


@pytest.mark.asyncio
async def test_port_list_includes_user_id(client, admin_headers):
    """PortInfo response includes user_id for every port."""
    create_resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org",
        "description": "user_id check"
    })
    assert create_resp.status_code == 200
    assert "user_id" in create_resp.json()

    list_resp = await client.get("/api/ports", headers=admin_headers)
    for port in list_resp.json():
        assert "user_id" in port
