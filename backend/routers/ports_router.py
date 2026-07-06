import ipaddress
import json
import logging
import random
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, defer
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
import database
from database import get_db, get_raw_connection, warn_fragmented_raw
from models import User, Port, Request as RequestModel
from schemas import PortCreate, PortUpdate, PortInfo, PortHistory, RequestInfo
from auth import require_approved, _verify_token_str, security_optional
from fastapi.security import HTTPAuthorizationCredentials
from config import ALLOW_INTERNAL_TARGETS, CLEANUP_BATCH_SIZE, CLEANUP_LOG_INTERVAL
from proxy_app import refresh_port_cache

logger = logging.getLogger("llm_proxy.ports")

router = APIRouter(prefix="/api/ports", tags=["ports"])

# ── One-time download ticket store (in-memory, no DB needed) ──
# Tickets allow browser-native <a> downloads without exposing the JWT
# in URL query strings (which would leak into nginx logs, browser
# history, and Referer headers).  Each ticket is single-use and
# expires after DOWNLOAD_TICKET_TTL seconds.
_DOWNLOAD_TICKET_TTL = 60
_export_tickets: dict[str, tuple[int, int, float]] = {}  # ticket → (port_id, user_id, expires_at)
_tickets_lock = Lock()


def _create_ticket(port_id: int, user_id: int) -> str:
    ticket = uuid.uuid4().hex
    with _tickets_lock:
        now = time.time()
        # Purge expired tickets (cheap — dict is small)
        expired = [t for t, (_, _, exp) in _export_tickets.items() if exp < now]
        for t in expired:
            del _export_tickets[t]
        _export_tickets[ticket] = (port_id, user_id, now + _DOWNLOAD_TICKET_TTL)
    return ticket


def _consume_ticket(ticket: str) -> tuple[int, int] | None:
    """Validate and consume a download ticket. Returns (port_id, user_id) or None."""
    with _tickets_lock:
        entry = _export_tickets.pop(ticket, None)
    if entry is None:
        return None
    port_id, user_id, expires_at = entry
    if expires_at < time.time():
        return None
    return (port_id, user_id)


def _is_private_ip(hostname: str) -> bool:
    """Check if a hostname resolves to a private/internal IP address."""
    # Direct IP check
    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        pass  # Not an IP, treat as hostname (expected for domain names like api.openai.com)

    # DNS resolution check
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return True
    except socket.gaierror:
        logger.warning("SSRF check: DNS resolution failed for '%s' — allowing (will fail at connect time if unreachable)", hostname)
        pass

    return False


def _validate_target_url(url: str) -> None:
    """Validate target URL and block SSRF attempts to internal networks."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Target URL must use http or https scheme")

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Target URL must have a valid hostname")

    # Block well-known internal hostnames
    blocked_hostnames = {"localhost", "metadata.google.internal", "metadata.google"}
    if hostname.lower() in blocked_hostnames:
        raise HTTPException(status_code=400, detail="Target URL cannot point to internal services")

    # Block private/internal IPs unless explicitly allowed
    if not ALLOW_INTERNAL_TARGETS and _is_private_ip(hostname):
        raise HTTPException(
            status_code=400,
            detail="Target URL cannot point to private/internal IP addresses. "
                   "Set ALLOW_INTERNAL_TARGETS=true in .env to override."
        )


# ── Background history-cleanup infrastructure ──────────────────────
# Similar to admin_router's port cleanup, but for clear_port_history
# which only deletes requests (NOT the port itself).
# Thread dedup prevents duplicate cleanups if the user clicks rapidly.
_history_cleanup_threads: dict[int, threading.Thread] = {}
_history_cleanup_lock = threading.Lock()
# Cap concurrent cleanup threads — shared with admin_router's cleanup
# to prevent exhausting MySQL connections when many cleanups fire at once.
_HISTORY_CLEANUP_SEMAPHORE = threading.BoundedSemaphore(3)


def _do_history_cleanup(port_id: int, port_number: int):
    """Batch-delete all requests for *port_id* on a background daemon thread.

    Unlike _do_port_cleanup in admin_router, this does NOT delete the port
    row — it only removes the request history.  If the process dies
    mid-cleanup, the remaining requests stay (user can retry).
    """
    logger.info(
        "[HistoryCleanup] Starting background history cleanup for port %s (id=%s)",
        port_number, port_id,
    )
    _HISTORY_CLEANUP_SEMAPHORE.acquire()
    try:
        raw_conn = get_raw_connection()
        try:
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
                            "[HistoryCleanup] Port %s: %s request(s) "
                            "removed so far...",
                            port_number, total_deleted,
                        )
                        time.sleep(0.1)
            # Check fragmentation after large deletions
            warn_fragmented_raw(raw_conn)
            logger.info(
                "[HistoryCleanup] Port %s: all %s request(s) deleted.",
                port_number, total_deleted,
            )
        except Exception:
            logger.exception(
                "[HistoryCleanup] Port %s (id=%s) history cleanup failed — "
                "remaining records can be cleared by retrying",
                port_number, port_id,
            )
        finally:
            try:
                raw_conn.close()
            except Exception:
                logger.warning(
                    "[HistoryCleanup] Port %s: failed to close raw connection "
                    "(already closed or broken)", port_number,
                )
    finally:
        _HISTORY_CLEANUP_SEMAPHORE.release()
        with _history_cleanup_lock:
            _history_cleanup_threads.pop(port_id, None)
            logger.debug(
                "[HistoryCleanup] Port %s: unregistered from cleanup tracker",
                port_number,
            )


@router.post("", response_model=PortInfo)
def create_port(
    data: PortCreate,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Create a new proxy for the current user (or a specified user if admin).

    In the shared-proxy architecture, this only creates a DB record.
    A random 5-digit proxy number is assigned (10000–99999).
    Retries up to 10 times on IntegrityError / collision.

    Admin users may specify ``user_id`` to create a proxy on behalf of
    another approved user.  Non-admin users cannot specify ``user_id``.
    """

    # ── Determine owner ──
    if data.user_id is not None:
        if current_user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Only administrators can assign proxies to other users"
            )
        owner = db.query(User).filter(User.id == data.user_id).first()
        if not owner:
            raise HTTPException(
                status_code=404,
                detail=f"User with id {data.user_id} not found"
            )
        if not owner.is_approved and owner.role != "admin":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot create proxy for unapproved user '{owner.username}'"
            )
        owner_id = owner.id
        owner_username = owner.username
    else:
        owner_id = current_user.id
        owner_username = current_user.username

    # In SQLAlchemy 2.0, session.commit() may release the connection back to
    # the pool, so the finally-block RELEASE_LOCK could land on a different
    # connection.  Get a dedicated raw connection for locking so GET_LOCK and
    # RELEASE_LOCK are guaranteed to use the same MySQL connection.
    raw_conn = db.bind.connect()
    lock_acquired = raw_conn.execute(
        text("SELECT GET_LOCK('llm_proxy_port_alloc', 10)")
    ).scalar()
    if lock_acquired != 1:
        raw_conn.close()
        raise HTTPException(
            status_code=503,
            detail="Server is busy processing port allocation, please try again"
        )

    try:
        # Query-based collision check — try random candidates, verify with DB.
        # Avoids loading all active port numbers into memory (O(n) problem
        # when there are tens of thousands of ports).
        assigned_port = None
        for _ in range(50):  # generous retry budget — 50 random tries is plenty
            candidate = random.randint(10000, 99999)
            conflict = db.execute(
                text(
                    "SELECT 1 FROM ports "
                    "WHERE port_number = :pn AND is_active = 1 AND deleted_at IS NULL "
                    "LIMIT 1"
                ),
                {"pn": candidate},
            ).first()
            if conflict is None:
                assigned_port = candidate
                break

        if assigned_port is None:
            raise HTTPException(
                status_code=503,
                detail="Unable to allocate a free proxy number. Please try again."
            )

        # Strip trailing slash from target_url
        target_url = data.target_url.rstrip("/")

        # Validate target URL (SSRF protection)
        _validate_target_url(target_url)

        # Create port record in DB (active immediately — shared proxy handles traffic)
        port = Port(
            port_number=assigned_port,
            user_id=owner_id,
            target_url=target_url,
            description=data.description,
            prefer_http2=data.prefer_http2,
            api_key=data.api_key or None,
            is_active=True,
        )
        db.add(port)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=503,
                detail="Proxy number conflict. Please try again."
            )
    finally:
        # Always release the MySQL named lock, even on error.
        raw_conn.execute(text("SELECT RELEASE_LOCK('llm_proxy_port_alloc')"))
        raw_conn.close()

    db.refresh(port)
    refresh_port_cache(db)

    return PortInfo(
        id=port.id,
        port_number=port.port_number,
        user_id=port.user_id,
        target_url=port.target_url,
        description=port.description or "",
        is_active=port.is_active,
        prefer_http2=port.prefer_http2,
        has_api_key=port.api_key is not None,
        created_at=port.created_at,
        request_count=0,
        username=owner_username,
    )


@router.get("", response_model=list[PortInfo])
def list_ports(
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 5000,
):
    """List ports — admin sees all users' ports, regular users see their own.

    Pagination via ``skip`` / ``limit`` query params.  Defaults to a large
    window so existing callers work unchanged; set lower limits for paginated UIs.
    """
    limit = min(max(limit, 1), 5000)
    if current_user.role == "admin":
        ports = db.query(Port).filter(Port.deleted_at.is_(None)).order_by(Port.created_at.desc()).offset(skip).limit(limit).all()
    else:
        ports = db.query(Port).filter(
            Port.user_id == current_user.id,
            Port.deleted_at.is_(None),
        ).order_by(Port.created_at.desc()).offset(skip).limit(limit).all()

    if not ports:
        return []

    # Batch-fetch request counts for all ports in a single query
    port_ids = [p.id for p in ports]
    count_rows = db.query(
        RequestModel.port_id, func.count(RequestModel.id)
    ).filter(
        RequestModel.port_id.in_(port_ids)
    ).group_by(RequestModel.port_id).all()
    count_map = {row[0]: row[1] for row in count_rows}

    # Batch-fetch creator usernames for all ports in a single query (admin only)
    username_map: dict[int, str] = {}
    if current_user.role == "admin":
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
            created_at=port.created_at,
            request_count=count_map.get(port.id, 0),
            username=username_map.get(port.user_id, ""),
        ))
    return result


@router.get("/active-ports", response_model=list[int])
def get_active_port_numbers(
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Get all active port numbers (admin sees all, users see theirs)."""
    if current_user.role == "admin":
        ports = db.query(Port.port_number).filter(
            Port.is_active.is_(True), Port.deleted_at.is_(None)
        ).all()
    else:
        ports = db.query(Port.port_number).filter(
            Port.user_id == current_user.id,
            Port.is_active.is_(True),
            Port.deleted_at.is_(None),
        ).all()
    return [p[0] for p in ports]


@router.get("/{port_id}")
def get_port_history(
    port_id: int,
    request: Request,
    since_id: int = 0,
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),  # for auth + port lookup + count only
):
    """Stream port history as NDJSON (one JSON object per line).

    Line 1: port metadata + total_requests
    Lines 2+: one request record each (response_body_raw excluded;
             use GET .../raw-sse to fetch on demand)

    The generator owns its own session so it outlives the route handler.
    Uses StreamSessionLocal (SSCursor) for true server-side streaming —
    yield_per(50) fetches rows incrementally instead of buffering all
    results in client memory.
    """
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # ── Count + creator on request-scoped session (cheap, happens once) ──
    request_count = db.query(func.count(RequestModel.id)).filter(
        RequestModel.port_id == port.id
    ).scalar() or 0

    creator = db.query(User).filter(User.id == port.user_id).first()
    creator_name = creator.username if creator else ""

    # Capture immutable values for the generator closure
    _port_dict = {
        "id": port.id,
        "port_number": port.port_number,
        "user_id": port.user_id,
        "target_url": port.target_url,
        "description": port.description or "",
        "is_active": port.is_active,
        "prefer_http2": port.prefer_http2,
        "has_api_key": port.api_key is not None,
        "deleted_at": port.deleted_at,
        "created_at": port.created_at,
        "request_count": request_count,
        "username": creator_name,
    }
    _port_id = port.id
    _limit = min(max(limit, 1), 100)
    _offset = offset
    _since_id = since_id

    def _record_to_dict(r):
        return {
            "id": r.id,
            "port_id": r.port_id,
            "method": r.method,
            "path": r.path,
            "request_headers": r.request_headers,
            "request_body": r.request_body,
            "response_headers": r.response_headers,
            "response_body": r.response_body,
            "status_code": r.status_code,
            "duration_ms": r.duration_ms,
            "reconstruction_error": r.reconstruction_error,
            "created_at": r.created_at,
        }

    async def stream_ndjson():
        own_db = database.StreamSessionLocal()
        try:
            query = (
                own_db.query(RequestModel)
                .options(defer(RequestModel.response_body_raw))
                .filter(RequestModel.port_id == _port_id)
            )
            if _since_id > 0:
                query = query.filter(RequestModel.id > _since_id)
            query = query.order_by(RequestModel.created_at.desc())
            if not (_since_id > 0):
                # Offset only makes sense without since_id filtering
                query = query.offset(_offset)
            query = query.limit(_limit)
            query = query.with_hint(RequestModel, "USE INDEX (ix_requests_port_created)")

            # Line 1 – port metadata
            yield (
                json.dumps(_port_dict, default=str, ensure_ascii=False) + "\n"
            ).encode("utf-8")

            # Subsequent lines – one record each
            _row_idx = 0
            for r in query.yield_per(50):
                _row_idx += 1
                rec = _record_to_dict(r)
                yield (
                    json.dumps(rec, default=str, ensure_ascii=False) + "\n"
                ).encode("utf-8")
                # Detect client disconnect every 20 rows → stop early,
                # so the DB connection is still healthy when we close it.
                if _row_idx % 20 == 0 and await request.is_disconnected():
                    logger.info("History client disconnected: port=%d rows=%d", _port_id, _row_idx)
                    return
        finally:
            try:
                own_db.close()
            except Exception:
                # Connection already broken (client disconnect / MySQL timeout).
                # Invalidate discards the raw DBAPI connection without rollback.
                try:
                    own_db.invalidate()
                except Exception:
                    logger.warning(
                        "Stream session cleanup failed for port=%d: "
                        "connection already destroyed, pool will discard on next use",
                        _port_id,
                    )

    return StreamingResponse(
        stream_ndjson(),
        media_type="application/x-ndjson",
    )


@router.delete("/{port_id}", response_model=dict)
def delete_port(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Soft-delete a proxy port — marks it deleted and inactive.

    The port becomes invisible to regular users but its data is preserved.
    Admins can view, restore, or permanently delete soft-deleted ports.
    """
    port = db.query(Port).filter(Port.id == port_id, Port.deleted_at.is_(None)).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    port_number = port.port_number
    port.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    port.is_active = False
    db.commit()

    refresh_port_cache(db)
    return {"message": f"Port {port_number} has been deleted."}


@router.post("/{port_id}/stop", response_model=dict)
def stop_port(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Deactivate a proxy port (shared proxy will reject traffic to it)."""
    port = db.query(Port).filter(Port.id == port_id, Port.deleted_at.is_(None)).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if not port.is_active:
        raise HTTPException(status_code=400, detail="Port is already stopped")

    port.is_active = False
    db.commit()

    refresh_port_cache(db)
    return {"message": f"Port {port.port_number} has been stopped."}


@router.post("/{port_id}/start", response_model=dict)
def start_port(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Reactivate a previously stopped proxy port."""
    port = db.query(Port).filter(Port.id == port_id, Port.deleted_at.is_(None)).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if port.is_active:
        raise HTTPException(status_code=400, detail="Port is already running")

    port.is_active = True
    db.commit()

    refresh_port_cache(db)
    return {"message": f"Port {port.port_number} has been started."}


@router.put("/{port_id}", response_model=PortInfo)
def update_port(
    port_id: int,
    data: PortUpdate,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Edit a port's description, target URL, port number, and/or owner (admin only).

    In the shared-proxy architecture, changes take effect immediately
    (no server restart needed). Target URL changes apply on the next request.

    Admin users may reassign the port to a different approved user via
    ``user_id``.  Non-admin users cannot change ownership.
    """
    port = db.query(Port).filter(Port.id == port_id, Port.deleted_at.is_(None)).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # ── Owner reassignment (admin-only) ──
    new_user_id = data.user_id
    if new_user_id is not None and new_user_id != port.user_id:
        if current_user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Only administrators can reassign ports to other users"
            )
        new_owner = db.query(User).filter(User.id == new_user_id).first()
        if not new_owner:
            raise HTTPException(
                status_code=404,
                detail=f"User with id {new_user_id} not found"
            )
        if not new_owner.is_approved and new_owner.role != "admin":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reassign port to unapproved user '{new_owner.username}'"
            )

    new_port_number = data.port_number
    _lock_raw_conn = None

    # ── Validate port_number change ──
    if new_port_number is not None and new_port_number != port.port_number:
        if new_port_number <= 0:
            raise HTTPException(
                status_code=400,
                detail="Port number must be a positive integer"
            )
        if not (10000 <= new_port_number <= 99999):
            raise HTTPException(
                status_code=400,
                detail="Port number must be a 5-digit integer between 10000 and 99999"
            )
        # Acquire MySQL lock on a dedicated raw connection (consistent with create_port)
        # so GET_LOCK and RELEASE_LOCK are guaranteed on the same MySQL connection
        # even if SQLAlchemy session.commit() recycles the session's connection.
        _lock_raw_conn = db.bind.connect()
        lock_acquired = _lock_raw_conn.execute(
            text("SELECT GET_LOCK('llm_proxy_port_alloc', 10)")
        ).scalar()
        if lock_acquired != 1:
            _lock_raw_conn.close()
            _lock_raw_conn = None
            raise HTTPException(
                status_code=503,
                detail="Server is busy processing port allocation, please try again"
            )

    try:
        # ── Check DB for conflicts (inside try so lock is released on error) ──
        if new_port_number is not None and new_port_number != port.port_number:
            existing = db.query(Port).filter(
                Port.port_number == new_port_number,
                Port.id != port.id,
                Port.is_active.is_(True),
                Port.deleted_at.is_(None),
            ).first()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"Port {new_port_number} is already assigned to another proxy."
                )

        # ── Apply changes ──
        if data.target_url is not None:
            port.target_url = data.target_url.rstrip("/")
            _validate_target_url(port.target_url)

        if data.description is not None:
            port.description = data.description

        if data.prefer_http2 is not None:
            port.prefer_http2 = data.prefer_http2

        # api_key: None=don't change, ""=clear (pass-through), non-empty=override
        if data.api_key is not None:
            port.api_key = data.api_key.strip() or None

        if new_port_number is not None and new_port_number != port.port_number:
            port.port_number = new_port_number

        # ── Reassign owner (admin-only, validated above) ──
        if new_user_id is not None and new_user_id != port.user_id:
            port.user_id = new_user_id

        # ── Commit changes ──
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Port number conflict — another request allocated this port."
            )

        refresh_port_cache(db)
    finally:
        if _lock_raw_conn is not None:
            try:
                _lock_raw_conn.execute(text("SELECT RELEASE_LOCK('llm_proxy_port_alloc')"))
            except Exception:
                logger.warning("Failed to release MySQL named lock for port update")
            finally:
                try:
                    _lock_raw_conn.close()
                except Exception:
                    pass

    # Get request count and owner username (not necessarily current_user for admin)
    owner = db.query(User).filter(User.id == port.user_id).first()
    owner_username = owner.username if owner else ""
    request_count = db.query(func.count(RequestModel.id)).filter(
        RequestModel.port_id == port.id
    ).scalar() or 0

    return PortInfo(
        id=port.id,
        port_number=port.port_number,
        user_id=port.user_id,
        target_url=port.target_url,
        description=port.description or "",
        is_active=port.is_active,
        prefer_http2=port.prefer_http2,
        has_api_key=port.api_key is not None,
        created_at=port.created_at,
        request_count=request_count,
        username=owner_username,
    )


@router.delete("/{port_id}/history", response_model=dict)
def clear_port_history(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Clear all request history for a port, but keep the port active.

    The actual batch-DELETE runs on a background daemon thread so large
    histories never block the FastAPI event loop.  If the process dies
    mid-cleanup, the remaining history stays (user can retry).
    """
    port = db.query(Port).filter(Port.id == port_id, Port.deleted_at.is_(None)).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Quick COUNT(*) — uses port_id index
    count = db.execute(
        text("SELECT COUNT(*) FROM requests WHERE port_id = :pid"),
        {"pid": port_id},
    ).scalar()

    if count == 0:
        return {"message": f"No history to clear for port {port.port_number}."}

    # Dedup: if a cleanup thread is already running for this port, reject.
    with _history_cleanup_lock:
        if port_id in _history_cleanup_threads:
            logger.warning(
                "History cleanup already in progress for port %s — skipping duplicate request",
                port.port_number,
            )
            return {
                "message":
                    f"History cleanup for port {port.port_number} "
                    f"is already running in background."
            }

    logger.info(
        "Clearing history for port %s: %s total request(s) — "
        "cleanup running in background.",
        port.port_number, count,
    )

    # Spawn background daemon thread — returns immediately
    t = threading.Thread(
        target=_do_history_cleanup,
        args=(port.id, port.port_number),
        daemon=True,
        name=f"history-cleanup-port-{port.port_number}",
    )
    with _history_cleanup_lock:
        _history_cleanup_threads[port_id] = t
    t.start()

    logger.info(
        "[HistoryCleanup] Spawned cleanup thread for port %s (%s records)",
        port.port_number, count,
    )

    return {
        "message":
            f"Clearing {count} request record(s) from port "
            f"{port.port_number} in background."
    }


@router.delete("/{port_id}/history/{request_id}", response_model=dict)
def delete_single_request(
    port_id: int,
    request_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Delete a single request record."""
    port = db.query(Port).filter(Port.id == port_id, Port.deleted_at.is_(None)).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    req_record = db.query(RequestModel).filter(
        RequestModel.id == request_id,
        RequestModel.port_id == port_id,
    ).first()
    if not req_record:
        raise HTTPException(status_code=404, detail="Request record not found")

    db.delete(req_record)
    db.commit()
    return {"message": "Request record deleted."}


@router.get("/{port_id}/history/{request_id}", response_model=RequestInfo)
def get_single_request(
    port_id: int,
    request_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Fetch a single request record by ID (for the tree-viewer page)."""
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    req_record = db.query(RequestModel).filter(
        RequestModel.id == request_id,
        RequestModel.port_id == port_id,
    ).first()
    if not req_record:
        raise HTTPException(status_code=404, detail="Request record not found")

    return RequestInfo(
        id=req_record.id,
        port_id=req_record.port_id,
        method=req_record.method,
        path=req_record.path,
        request_headers=req_record.request_headers,
        request_body=req_record.request_body,
        response_headers=req_record.response_headers,
        response_body=req_record.response_body,
        response_body_raw=req_record.response_body_raw,
        status_code=req_record.status_code,
        duration_ms=req_record.duration_ms,
        created_at=req_record.created_at,
    )


@router.get("/{port_id}/history/{request_id}/raw-sse")
def get_raw_sse(
    port_id: int,
    request_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Fetch only the response_body_raw (original SSE text) for one request.

    This is a lightweight endpoint called on-demand when the user clicks
    "查看原始SSE" in the frontend.  The main list query defers this
    column to keep the list view fast.
    """
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    row = (
        db.query(RequestModel.response_body_raw)
        .filter(RequestModel.id == request_id, RequestModel.port_id == port_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Request record not found")

    return {"raw_sse": row[0] or ""}


@router.post("/{port_id}/export-ticket")
def create_export_ticket(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Create a one-time download ticket for browser-native export.

    Browser <a> tag downloads cannot carry an Authorization header, so
    we issue a short-lived single-use ticket that the browser appends as
    ``?ticket=...``.  The real JWT never appears in URLs, nginx logs, or
    browser history.
    """
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")
    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    ticket = _create_ticket(port_id, current_user.id)
    return {"ticket": ticket, "expires_in": _DOWNLOAD_TICKET_TTL}


@router.get("/{port_id}/export")
def export_port_history(
    port_id: int,
    request: Request,
    method_filter: str = "all",
    format: str = "full",
    ticket: str = None,
    credentials: HTTPAuthorizationCredentials = Depends(security_optional),
    db: Session = Depends(get_db),
):
    """Export all request history for a port as a streaming JSON download.

    Supports two auth modes so the browser can trigger a direct download
    (where custom headers are impossible — use ``?ticket=...`` from
    ``POST /{port_id}/export-ticket``) while the fetch-based path keeps
    using the Bearer header.

    Args:
        method_filter: ``all`` (default) or ``api`` (POST/PUT/PATCH/DELETE only).
        format: ``full`` (default, all fields + port metadata) or
                ``simple`` (flat array of {index, method, path, status_code,
                request, response} — no headers, no port wrapper).
        ticket: One-time download ticket (from POST /{port_id}/export-ticket).
    """
    # ── Auth: prefer Bearer header, fallback to ?ticket= one-time token ──
    _req_start_ts = time.time()
    logger.info(
        "Export request received: port=%d method_filter=%s format=%s auth_mode=%s",
        port_id, method_filter, format,
        "bearer" if credentials else ("ticket" if ticket else "none"),
    )

    _t_auth = time.time()
    if credentials:
        current_user = _verify_token_str(credentials.credentials, db)
        logger.info(
            "Export auth: mode=bearer user=%d role=%s elapsed=%.3fs",
            current_user.id, current_user.role, time.time() - _t_auth,
        )
    elif ticket:
        entry = _consume_ticket(ticket)
        if entry is None:
            logger.warning("Export auth: ticket invalid or expired")
            raise HTTPException(status_code=401, detail="Invalid or expired ticket")
        _ticket_port_id, _ticket_user_id = entry
        if _ticket_port_id != port_id:
            logger.warning(
                "Export auth: ticket port mismatch ticket_port=%d request_port=%d",
                _ticket_port_id, port_id,
            )
            raise HTTPException(status_code=403, detail="Ticket does not match port")
        current_user = db.query(User).filter(User.id == _ticket_user_id).first()
        if not current_user:
            logger.warning("Export auth: ticket user not found user=%d", _ticket_user_id)
            raise HTTPException(status_code=401, detail="User not found")
        logger.info(
            "Export auth: mode=ticket user=%d role=%s elapsed=%.3fs",
            current_user.id, current_user.role, time.time() - _t_auth,
        )
    else:
        logger.warning("Export auth: no credentials provided")
        raise HTTPException(status_code=401, detail="Authentication required")

    if not current_user.is_approved and current_user.role != "admin":
        logger.warning("Export auth: user=%d not approved", current_user.id)
        raise HTTPException(status_code=403, detail="Account not yet approved by admin")

    _t_port = time.time()
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        logger.warning("Export: port=%d not found", port_id)
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        logger.warning(
            "Export: access denied port=%d owner=%d user=%d",
            port_id, port.user_id, current_user.id,
        )
        raise HTTPException(status_code=403, detail="Access denied")
    logger.info(
        "Export port lookup: port_number=%d target=%s elapsed=%.3fs",
        port.port_number, port.target_url, time.time() - _t_port,
    )

    # Capture immutable values for use inside the generator
    _port_number = port.port_number
    _port_target = port.target_url
    _port_desc = port.description
    _port_created = port.created_at.isoformat() if port.created_at else None
    _simple = (format == "simple")

    # Build filename for Content-Disposition
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filter_label = "-api" if method_filter == "api" else ""
    fmt_label = "-simple" if _simple else ""
    filename = f"llm-proxy-port{_port_number}{filter_label}{fmt_label}-{ts}.json"

    logger.info(
        "Export setup: port=%d format=%s filename=%s",
        _port_number, "simple" if _simple else "full", filename,
    )

    # ── JSON building helpers (module-level, outside the generator) ──

    def _json_literal(v):
        """Encode a Python scalar as a JSON literal (no quotes for numbers/null)."""
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        return json.dumps(v, ensure_ascii=False)

    def _embed_body(raw):
        """Embed a raw body/header string as a JSON value.

        Body fields may not always be valid JSON — _serialize_body() can
        return raw text (non-JSON UTF-8) or a binary-data placeholder like
        ``[binary data, 100 bytes]`` which starts with ``[`` but is NOT
        valid JSON.  We validate with json.loads(): valid JSON embeds
        directly (zero re-serialization); everything else gets wrapped via
        json.dumps() so the export is always well-formed JSON.
        """
        if not raw:
            return "null"
        try:
            json.loads(raw)
            return raw                       # valid JSON — embed as-is
        except (json.JSONDecodeError, ValueError):
            return json.dumps(raw, ensure_ascii=False)  # plain text — wrap as string

    def _build_full_row(r):
        """Build one row's JSON bytes directly — no intermediate dict, no json.loads.

        The body/header fields in MySQL are usually valid JSON strings
        (they come from LLM API request/response bodies).  _embed_body()
        does a fast structural check and embeds valid JSON directly,
        falling back to json.dumps() for non-JSON text.  This eliminates
        the dominant CPU cost of large exports: json.loads() per row.
        """
        parts = [
            b'{"id":', str(r.id).encode(),
            b',"method":', json.dumps(r.method, ensure_ascii=False).encode(),
            b',"path":', json.dumps(r.path, ensure_ascii=False).encode(),
            b',"status_code":', _json_literal(r.status_code).encode(),
            b',"duration_ms":', _json_literal(r.duration_ms).encode(),
            b',"reconstruction_error":', json.dumps(r.reconstruction_error, ensure_ascii=False).encode(),
            b',"timestamp":',
            json.dumps(r.created_at.isoformat() if r.created_at else None, ensure_ascii=False).encode(),
        ]
        for field in ("request_headers", "request_body", "response_headers", "response_body"):
            raw = getattr(r, field, None)
            parts.append(b',"')
            parts.append(field.encode())
            parts.append(b'":')
            parts.append(_embed_body(raw).encode())
        parts.append(b'}')
        return b"".join(parts)

    def _build_simple_row(r, index):
        """Build one simple-format row's JSON bytes directly."""
        parts = [
            b'{"index":', str(index).encode(),
            b',"method":', json.dumps(r.method, ensure_ascii=False).encode(),
            b',"path":', json.dumps(r.path, ensure_ascii=False).encode(),
            b',"status_code":', _json_literal(r.status_code).encode(),
        ]
        for field in ("request_body", "response_body"):
            key = "request" if field == "request_body" else "response"
            raw = getattr(r, field, None)
            parts.append(b',"')
            parts.append(key.encode())
            parts.append(b'":')
            parts.append(_embed_body(raw).encode())
        parts.append(b'}')
        return b"".join(parts)

    async def stream_jsonl():
        own_db = database.StreamSessionLocal()
        _row_count = 0
        try:
            # Build query, deferring columns the output format does not need.
            _deferred = [RequestModel.response_body_raw]  # never needed for export
            if _simple:
                _deferred.extend([
                    RequestModel.id,
                    RequestModel.port_id,
                    RequestModel.duration_ms,
                    RequestModel.reconstruction_error,
                    RequestModel.created_at,
                    RequestModel.request_headers,
                    RequestModel.response_headers,
                ])
            query = (
                own_db.query(RequestModel)
                .options(*[defer(col) for col in _deferred])
                .filter(RequestModel.port_id == port_id)
            )
            if method_filter == "api":
                query = query.filter(RequestModel.method.in_(["POST", "PUT", "PATCH", "DELETE"]))
            query = query.order_by(RequestModel.created_at.asc())
            query = query.with_hint(RequestModel, "USE INDEX (ix_requests_port_created)")

            logger.info("Export stream started: port=%d", _port_number)

            if _simple:
                # Flat array: [{"index":1,...}, ...]
                yield b"["
                first = True
                idx = 0
                try:
                    for r in query.yield_per(500):
                        idx += 1
                        _row_count = idx
                        if not first:
                            yield b","
                        first = False
                        yield _build_simple_row(r, idx)
                        # Heartbeat: check client connection every 100 rows
                        if idx % 100 == 0 and await request.is_disconnected():
                            logger.info("Export client disconnected: port=%d rows=%d", _port_number, _row_count)
                            return
                    yield b"]"
                except GeneratorExit:
                    logger.info("Export cancelled by client: port=%d rows=%d", _port_number, _row_count)
                    return
                except Exception as _export_err:
                    logger.error("Export interrupted: port=%d %d rows — %s: %s",
                                 _port_number, _row_count, type(_export_err).__name__, _export_err)
                    if _row_count > 0:
                        yield b","
                    _err_obj = json.dumps({
                        "_export_error": "incomplete",
                        "rows_received": _row_count,
                        "error": f"{type(_export_err).__name__}: {_export_err}",
                    }, ensure_ascii=False).encode("utf-8")
                    yield _err_obj
                    yield b"]"
            else:
                # Full: {"port":{...},"requests":[...]}
                yield b'{"port":'
                yield json.dumps({
                    "port_number": _port_number,
                    "target_url": _port_target,
                    "description": _port_desc,
                    "created_at": _port_created,
                }, ensure_ascii=False).encode("utf-8")
                yield b',"requests":['

                first = True
                row = 0
                try:
                    for r in query.yield_per(500):
                        row += 1
                        _row_count = row
                        if not first:
                            yield b","
                        first = False
                        yield _build_full_row(r)
                        # Heartbeat: check client connection every 100 rows
                        if row % 100 == 0 and await request.is_disconnected():
                            logger.info("Export client disconnected: port=%d rows=%d", _port_number, _row_count)
                            return
                    yield b"]}"
                except GeneratorExit:
                    logger.info("Export cancelled by client: port=%d rows=%d", _port_number, _row_count)
                    return
                except Exception as _export_err:
                    logger.error("Export interrupted: port=%d %d rows — %s: %s",
                                 _port_number, _row_count, type(_export_err).__name__, _export_err)
                    if _row_count > 0:
                        yield b","
                    _err_obj = json.dumps({
                        "_export_error": "incomplete",
                        "rows_received": _row_count,
                        "error": f"{type(_export_err).__name__}: {_export_err}",
                    }, ensure_ascii=False).encode("utf-8")
                    yield _err_obj
                    yield b'],"_export_error":"incomplete"}'

            logger.info("Export finished: port=%d %d rows", _port_number, _row_count)
        finally:
            try:
                own_db.close()
            except Exception:
                try:
                    own_db.invalidate()
                except Exception:
                    logger.warning(
                        "Export stream cleanup failed for port=%d: "
                        "connection already destroyed, pool will discard on next use",
                        _port_number,
                    )

    return StreamingResponse(
        stream_jsonl(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
