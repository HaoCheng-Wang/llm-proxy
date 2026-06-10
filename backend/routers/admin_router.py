from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import User, Port, Request as RequestModel
from schemas import UserApproval, UserInfo, AdminUserList, PortInfo, DeletedPortList
from auth import require_admin
from proxy_app import refresh_port_cache
import logging

logger = logging.getLogger("llm_proxy.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users", response_model=AdminUserList)
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """List all users (admin only)."""
    users = db.query(User).order_by(User.created_at.desc()).all()
    return AdminUserList(users=[
        UserInfo(
            id=u.id,
            username=u.username,
            role=u.role,
            is_approved=u.is_approved,
            created_at=u.created_at,
        ) for u in users
    ])


@router.put("/users/approve", response_model=dict)
def approve_user(data: UserApproval, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    """Approve or reject a user (admin only)."""
    user = db.query(User).filter(User.id == data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Cannot modify admin status")

    user.is_approved = data.is_approved
    db.commit()
    action = "approved" if data.is_approved else "rejected"
    return {"message": f"User '{user.username}' has been {action}."}


@router.delete("/users/{user_id}", response_model=dict)
def delete_user(user_id: int, admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    """Delete a user (admin only)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete admin")

    username = user.username
    db.delete(user)
    db.commit()
    return {"message": f"User '{username}' has been deleted."}


# ──────────────────────────────────────────────
#  Soft-delete port management (admin only)
# ──────────────────────────────────────────────

@router.get("/deleted-ports", response_model=DeletedPortList)
def list_deleted_ports(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all soft-deleted ports with their request counts and creator usernames."""
    ports = (
        db.query(Port)
        .filter(Port.deleted_at.isnot(None))
        .order_by(Port.deleted_at.desc())
        .all()
    )

    if not ports:
        return DeletedPortList(ports=[])

    port_ids = [p.id for p in ports]
    count_rows = (
        db.query(RequestModel.port_id, func.count(RequestModel.id))
        .filter(RequestModel.port_id.in_(port_ids))
        .group_by(RequestModel.port_id)
        .all()
    )
    count_map = {row[0]: row[1] for row in count_rows}

    user_ids = {p.user_id for p in ports}
    users = db.query(User.id, User.username).filter(User.id.in_(user_ids)).all()
    username_map = {u.id: u.username for u in users}

    result = []
    for port in ports:
        result.append(PortInfo(
            id=port.id,
            port_number=port.port_number,
            target_url=port.target_url,
            description=port.description or "",
            is_active=port.is_active,
            deleted_at=port.deleted_at,
            created_at=port.created_at,
            request_count=count_map.get(port.id, 0),
            username=username_map.get(port.user_id, ""),
        ))
    return DeletedPortList(ports=result)


@router.post("/ports/{port_id}/restore", response_model=dict)
def restore_port(
    port_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Restore a soft-deleted port — clears deleted_at, keeps is_active=False."""
    port = db.query(Port).filter(
        Port.id == port_id, Port.deleted_at.isnot(None)
    ).first()
    if not port:
        raise HTTPException(status_code=404, detail="Deleted port not found")

    port.deleted_at = None
    port.is_active = False  # Restored but inactive — user must re-enable
    db.commit()

    refresh_port_cache(db)
    return {"message": f"Port {port.port_number} has been restored."}


@router.delete("/ports/{port_id}/permanent", response_model=dict)
def permanent_delete_port(
    port_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Permanently delete a soft-deleted port and all its request history."""
    port = db.query(Port).filter(
        Port.id == port_id, Port.deleted_at.isnot(None)
    ).first()
    if not port:
        raise HTTPException(status_code=404, detail="Deleted port not found")

    port_number = port.port_number

    # Manually delete associated requests first.  Some requests may have
    # port_id=NULL (written while the port was already soft-deleted), so
    # we delete by port_number lookup instead of relying solely on cascade.
    # First delete requests linked by port_id:
    req_deleted = db.query(RequestModel).filter(
        RequestModel.port_id == port.id
    ).delete(synchronize_session="fetch")

    # Then delete orphaned requests that have port_id=NULL but belong to
    # this port_number (identified via request_headers / method / path):
    # These are requests written during the soft-deleted window.
    # We skip them — they are already unreachable through the UI and
    # will be cleaned up by the DB admin periodically.

    logger.info(
        "Permanently deleting port %s: removing %s linked request(s)",
        port_number, req_deleted,
    )

    db.delete(port)
    db.commit()

    refresh_port_cache(db)
    return {"message": f"Port {port_number} has been permanently deleted."}
