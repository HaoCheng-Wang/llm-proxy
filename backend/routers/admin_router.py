from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from database import get_db, get_raw_connection, warn_fragmented_raw
from models import User, Port, Request as RequestModel
from schemas import UserApproval, UserInfo, AdminUserList, PortInfo, DeletedPortList
from auth import require_admin
from proxy_app import refresh_port_cache
from config import CLEANUP_BATCH_SIZE, CLEANUP_LOG_INTERVAL
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("llm_proxy.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── Background port cleanup infrastructure ──────────────────────────
# Dual-channel cleanup:
#   Fast path  → daemon thread with dedicated pymysql connection
#   Guaranteed → MySQL Event "evt_cleanup_flagged_ports" (every 30s)
# The thread tries to delete immediately; if the process dies, the
# MySQL Event nibble-deletes until the port is gone.  On restart,
# resume_port_cleanups() re-spawns threads for any remaining ports.
_cleanup_threads: dict[int, threading.Thread] = {}
_cleanup_lock = threading.Lock()
# Cap concurrent cleanup threads to avoid exhausting MySQL connections
# and OS file descriptors when a user with many ports is deleted.
_MAX_CONCURRENT_CLEANUPS = 5
_cleanup_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_CLEANUPS)


def _do_port_cleanup(port_id: int, port_number: int):
    """Batch-delete requests for *port_id*, then delete the port row.

    Runs on a daemon thread via its own pymysql connection.
    Also handles the case where the MySQL Event already cleaned up.
    """
    logger.info(
        "[Cleanup] Starting background cleanup for port %s (id=%s)",
        port_number, port_id,
    )
    # Throttle concurrent cleanups — the semaphore prevents exhausting
    # MySQL connections and OS file descriptors when deleting a user
    # who owns hundreds of ports.
    _cleanup_semaphore.acquire()
    try:
        raw_conn = get_raw_connection()
        try:
            # Check if port still exists (MySQL Event may have finished it)
            with raw_conn.cursor() as cur:
                cur.execute("SELECT id FROM ports WHERE id = %s", (port_id,))
                if cur.fetchone() is None:
                    logger.info(
                        "[Cleanup] Port %s already cleaned up by MySQL Event — nothing to do",
                        port_number,
                    )
                    return

            total_deleted = 0
            batch_num = 0
            with raw_conn.cursor() as cur:
                while True:
                    cur.execute(
                        "DELETE FROM requests WHERE port_id = %s LIMIT %s",
                        (port_id, CLEANUP_BATCH_SIZE),
                    )
                    n = cur.rowcount
                    if n == 0:
                        break
                    total_deleted += n
                    batch_num += 1
                    raw_conn.commit()
                    if batch_num % CLEANUP_LOG_INTERVAL == 0:
                        logger.info(
                            "[Cleanup] Port %s: %s request(s) removed so far...",
                            port_number, total_deleted,
                        )
                        # Brief pause to avoid overwhelming MySQL I/O
                        time.sleep(0.1)
            warn_fragmented_raw(raw_conn)
            # Delete the port record (Event may have beaten us — that's fine)
            with raw_conn.cursor() as cur:
                cur.execute("DELETE FROM ports WHERE id = %s", (port_id,))
                raw_conn.commit()

            logger.info(
                "[Cleanup] Port %s: all %s request(s) + port record deleted.",
                port_number, total_deleted,
            )
        except Exception:
            logger.exception(
                "[Cleanup] Port %s (id=%s) cleanup failed — MySQL Event will retry",
                port_number, port_id,
            )
        finally:
            raw_conn.close()
    finally:
        _cleanup_semaphore.release()
        with _cleanup_lock:
            _cleanup_threads.pop(port_id, None)


def resume_port_cleanups():
    """Startup hook: re-spawn daemon threads for any ports still flagged.
    This is the *fast path* — the MySQL Event is the guaranteed fallback."""
    db = next(get_db())
    try:
        rows = db.execute(
            text("SELECT id, port_number FROM ports WHERE cleaning_started_at IS NOT NULL")
        ).fetchall()
        if not rows:
            return
        logger.info(
            "Resuming cleanup for %s port(s) (MySQL Event also handles these)...",
            len(rows),
        )
        for pid, pnum in rows:
            _spawn_cleanup_thread(pid, pnum)
    finally:
        db.close()


def _spawn_cleanup_thread(port_id: int, port_number: int):
    """Spawn a daemon thread for *port_id*, deduplicating by id."""
    with _cleanup_lock:
        if port_id in _cleanup_threads:
            return  # already cleaning
        t = threading.Thread(
            target=_do_port_cleanup,
            args=(port_id, port_number),
            daemon=True,
            name=f"cleanup-port-{port_number}",
        )
        _cleanup_threads[port_id] = t
    t.start()


@router.get("/users", response_model=AdminUserList)
def list_users(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 2000,
):
    """List all users (admin only).

    Pagination via ``skip`` / ``limit`` query params so the endpoint
    does not break when there are thousands of users.
    """
    limit = min(max(limit, 1), 2000)
    users = db.query(User).order_by(User.created_at.desc()).offset(skip).limit(limit).all()
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
    """Delete a user (admin only).

    Instead of relying on SQLAlchemy cascade (which loads all child rows
    into memory), all of the user's ports are flagged for background
    cleanup.  The user row itself is deleted immediately so the account
    is gone; background daemon threads (and the MySQL Event as fallback)
    then batch-delete the port and request rows safely.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete admin")

    username = user.username

    # Flag all ports for background cleanup.
    # IMPORTANT: only set the flags here — threads are spawned AFTER commit
    # so that a rollback does not leave orphaned threads deleting live data.
    ports = (
        db.query(Port)
        .filter(Port.user_id == user_id, Port.cleaning_started_at.is_(None))
        .all()
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for port in ports:
        port.is_active = False
        port.deleted_at = now
        port.cleaning_started_at = now

    # Delete the user row immediately (ports will be cleaned in background)
    db.delete(user)
    db.commit()

    refresh_port_cache(db)

    # Spawn cleanup threads AFTER successful commit.
    # If commit failed, cleaning_started_at was never persisted → threads
    # won't find the ports flagged and will exit immediately.
    for port in ports:
        _spawn_cleanup_thread(port.id, port.port_number)
        logger.info(
            "[Cleanup] Spawned cleanup thread for port %s (user '%s' deleted)",
            port.port_number, username,
        )

    logger.info(
        "User '%s' (id=%d) deleted — %d port(s) scheduled for background cleanup.",
        username, user_id, len(ports),
    )
    return {
        "message":
            f"User '{username}' has been deleted. "
            f"{len(ports)} port(s) are being cleaned up in background."
    }


# ──────────────────────────────────────────────
#  Soft-delete port management (admin only)
# ──────────────────────────────────────────────

@router.get("/deleted-ports", response_model=DeletedPortList)
def list_deleted_ports(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 5000,
):
    """List all soft-deleted ports with their request counts and creator usernames.

    Pagination via ``skip`` / ``limit`` query params.  Defaults to a large
    window so existing callers work unchanged.
    """
    limit = min(max(limit, 1), 5000)
    ports = (
        db.query(Port)
        .filter(
            Port.deleted_at.isnot(None),
            Port.cleaning_started_at.is_(None),  # hide ports being cleaned
        )
        .order_by(Port.deleted_at.desc())
        .offset(skip)
        .limit(limit)
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
            user_id=port.user_id,
            target_url=port.target_url,
            description=port.description or "",
            is_active=port.is_active,
            prefer_http2=port.prefer_http2,
            has_api_key=port.api_key is not None,
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
    """Restore a soft-deleted port — clears deleted_at, keeps is_active=False.

    Checks for port_number conflicts with active ports before restoring,
    since another port may have been assigned the same number after this
    one was deleted.
    """
    port = db.query(Port).filter(
        Port.id == port_id, Port.deleted_at.isnot(None)
    ).first()
    if not port:
        raise HTTPException(status_code=404, detail="Deleted port not found")

    # Check if the port_number is now occupied by another active port
    conflict = db.query(Port).filter(
        Port.port_number == port.port_number,
        Port.id != port.id,
        Port.is_active.is_(True),
        Port.deleted_at.is_(None),
    ).first()
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot restore: port {port.port_number} is already in use "
                   f"by another active proxy."
        )

    port.deleted_at = None
    port.cleaning_started_at = None  # clear any stale cleanup flag
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
    """Permanently delete a soft-deleted port and all its request history.

    The port is flagged for cleanup immediately (202 Accepted returned to
    the frontend), and the actual batch-DELETE runs on a background daemon
    thread with a dedicated long-timeout pymysql connection.  If the
    process dies mid-cleanup, resume_port_cleanups() re-spawns threads
    on the next startup for every flagged port.
    """
    port = db.query(Port).filter(
        Port.id == port_id, Port.deleted_at.isnot(None)
    ).first()
    if not port:
        raise HTTPException(status_code=404, detail="Deleted port not found")

    port_number = port.port_number

    # Already being cleaned?
    if port.cleaning_started_at is not None:
        return {"message": f"Port {port_number} cleanup is already in progress."}

    # Flag for cleanup and remove from proxy routing immediately.
    port.cleaning_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    refresh_port_cache(db)

    # Quick COUNT(*) for the log line.
    count = db.execute(
        text("SELECT COUNT(*) FROM requests WHERE port_id = :pid"),
        {"pid": port.id},
    ).scalar()

    logger.info(
        "Permanently deleting port %s: %s total request(s) — "
        "cleanup running in background.",
        port_number, count,
    )

    # Spawn background thread.
    _spawn_cleanup_thread(port.id, port_number)

    return {"message": f"Port {port_number} cleanup has been started."}
