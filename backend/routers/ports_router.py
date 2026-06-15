import ipaddress
import json
import socket
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, defer
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
import database
from database import get_db
from models import User, Port, Request as RequestModel
from schemas import PortCreate, PortUpdate, PortInfo, PortHistory, RequestInfo
from auth import require_approved, _verify_token_str, security_optional
from fastapi.security import HTTPAuthorizationCredentials
from config import ALLOW_INTERNAL_TARGETS
from proxy_app import refresh_port_cache

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
        pass  # Not an IP, treat as hostname

    # DNS resolution check
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return True
    except socket.gaierror:
        pass  # DNS resolution failed, allow it (will fail at connect time)

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


@router.post("", response_model=PortInfo)
def create_port(
    data: PortCreate,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Create a new proxy for the current user.

    In the shared-proxy architecture, this only creates a DB record.
    A random 5-digit proxy number is assigned (10000–99999).
    Retries up to 10 times on IntegrityError / collision.
    """
    import random

    # Acquire a MySQL named lock to serialize port allocation.
    lock_acquired = db.execute(
        text("SELECT GET_LOCK('llm_proxy_port_alloc', 10)")
    ).scalar()
    if lock_acquired != 1:
        raise HTTPException(
            status_code=503,
            detail="Server is busy processing port allocation, please try again"
        )

    try:
        # Get all currently-active port numbers to avoid assignment collisions
        active_numbers = set(
            p[0] for p in db.query(Port.port_number)
            .filter(Port.is_active.is_(True), Port.deleted_at.is_(None)).all()
        )

        # Random 5-digit number, retry with new random if collision
        assigned_port = None
        for _ in range(10):
            candidate = random.randint(10000, 99999)
            if candidate not in active_numbers:
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
            user_id=current_user.id,
            target_url=target_url,
            description=data.description,
            prefer_http2=data.prefer_http2,
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
        db.execute(text("SELECT RELEASE_LOCK('llm_proxy_port_alloc')"))

    db.refresh(port)
    refresh_port_cache(db)

    return PortInfo(
        id=port.id,
        port_number=port.port_number,
        target_url=port.target_url,
        description=port.description or "",
        is_active=port.is_active,
        prefer_http2=port.prefer_http2,
        created_at=port.created_at,
        request_count=0,
        username=current_user.username,
    )


@router.get("", response_model=list[PortInfo])
def list_ports(
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """List ports — admin sees all users' ports, regular users see their own."""
    if current_user.role == "admin":
        ports = db.query(Port).filter(Port.deleted_at.is_(None)).order_by(Port.created_at.desc()).all()
    else:
        ports = db.query(Port).filter(
            Port.user_id == current_user.id,
            Port.deleted_at.is_(None),
        ).order_by(Port.created_at.desc()).all()

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
            target_url=port.target_url,
            description=port.description or "",
            is_active=port.is_active,
            prefer_http2=port.prefer_http2,
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
    yield_per(50) keeps memory constant regardless of total record count.
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
        "target_url": port.target_url,
        "description": port.description or "",
        "is_active": port.is_active,
        "prefer_http2": port.prefer_http2,
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

    def stream_ndjson():
        own_db = database.SessionLocal()
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

            # Line 1 – port metadata
            yield (
                json.dumps(_port_dict, default=str, ensure_ascii=False) + "\n"
            ).encode("utf-8")

            # Subsequent lines – one record each
            for r in query.yield_per(50):
                rec = _record_to_dict(r)
                yield (
                    json.dumps(rec, default=str, ensure_ascii=False) + "\n"
                ).encode("utf-8")
        finally:
            own_db.close()

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
    """Edit a port's description, target URL, and/or port number.

    In the shared-proxy architecture, changes take effect immediately
    (no server restart needed). Target URL changes apply on the next request.
    """
    port = db.query(Port).filter(Port.id == port_id, Port.deleted_at.is_(None)).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    new_port_number = data.port_number
    _port_lock_held = False

    # ── Validate port_number change ──
    if new_port_number is not None and new_port_number != port.port_number:
        if new_port_number <= 0:
            raise HTTPException(
                status_code=400,
                detail="Port number must be a positive integer"
            )
        # Acquire MySQL lock to prevent concurrent update races
        lock_acquired = db.execute(
            text("SELECT GET_LOCK('llm_proxy_port_alloc', 10)")
        ).scalar()
        if lock_acquired != 1:
            raise HTTPException(
                status_code=503,
                detail="Server is busy processing port allocation, please try again"
            )
        _port_lock_held = True

        # Check DB for conflicts
        existing = db.query(Port).filter(
            Port.port_number == new_port_number,
            Port.id != port.id
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Port {new_port_number} is already assigned to another proxy."
            )

    try:
        # ── Apply changes ──
        if data.target_url is not None:
            port.target_url = data.target_url.rstrip("/")
            _validate_target_url(port.target_url)

        if data.description is not None:
            port.description = data.description

        if data.prefer_http2 is not None:
            port.prefer_http2 = data.prefer_http2

        if new_port_number is not None and new_port_number != port.port_number:
            port.port_number = new_port_number

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
        if _port_lock_held:
            db.execute(text("SELECT RELEASE_LOCK('llm_proxy_port_alloc')"))

    # Get request count
    request_count = db.query(func.count(RequestModel.id)).filter(
        RequestModel.port_id == port.id
    ).scalar() or 0

    return PortInfo(
        id=port.id,
        port_number=port.port_number,
        target_url=port.target_url,
        description=port.description or "",
        is_active=port.is_active,
        prefer_http2=port.prefer_http2,
        created_at=port.created_at,
        request_count=request_count,
        username=current_user.username,
    )


@router.delete("/{port_id}/history", response_model=dict)
def clear_port_history(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Clear all request history for a port, but keep the port active."""
    port = db.query(Port).filter(Port.id == port_id, Port.deleted_at.is_(None)).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    deleted_count = db.query(RequestModel).filter(
        RequestModel.port_id == port_id
    ).delete()
    db.commit()

    return {"message": f"Cleared {deleted_count} request records from port {port.port_number}."}


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
    if credentials:
        current_user = _verify_token_str(credentials.credentials, db)
    elif ticket:
        entry = _consume_ticket(ticket)
        if entry is None:
            raise HTTPException(status_code=401, detail="Invalid or expired ticket")
        _ticket_port_id, _ticket_user_id = entry
        if _ticket_port_id != port_id:
            raise HTTPException(status_code=403, detail="Ticket does not match port")
        current_user = db.query(User).filter(User.id == _ticket_user_id).first()
        if not current_user:
            raise HTTPException(status_code=401, detail="User not found")
    else:
        raise HTTPException(status_code=401, detail="Authentication required")

    if not current_user.is_approved and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Account not yet approved by admin")

    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # ── Count total on the request-scoped session (cheap) ──
    total_q = db.query(func.count(RequestModel.id)).filter(
        RequestModel.port_id == port.id
    )
    if method_filter == "api":
        total_q = total_q.filter(RequestModel.method.in_(["POST", "PUT", "PATCH", "DELETE"]))
    total_count = total_q.scalar() or 0

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

        The text stored in MySQL LONGTEXT columns is already valid JSON
        (LLM API request/response bodies are always JSON).  We verify with
        a single-character structural check and embed directly — no
        json.loads()/json.dumps() round-trip.
        """
        if not raw:
            return "null"
        c = raw[0]
        # JSON must start with: { [ " t f n - 0-9
        if c in '{}[]"' or c in ('t', 'f', 'n') or c == '-' or c.isdigit():
            return raw
        # Rare edge case: leading whitespace — strip and re-check
        s = raw.lstrip()
        if s and s[0] in '{}[]"tfn-0123456789':
            return raw
        # Non-JSON body (e.g. plain text, form data) — encode as JSON string
        return json.dumps(raw, ensure_ascii=False)

    def _build_full_row(r):
        """Build one row's JSON bytes directly — no intermediate dict, no json.loads.

        The body/header fields in MySQL are already valid JSON strings
        (they come from LLM API request/response bodies).  We verify with a
        single-character structural check and embed directly, eliminating the
        dominant CPU cost of large exports: json.loads() + json.dumps() per row.
        """
        parts = [
            b'{"id":', str(r.id).encode(),
            b',"method":', json.dumps(r.method, ensure_ascii=False).encode(),
            b',"path":', json.dumps(r.path, ensure_ascii=False).encode(),
            b',"status_code":', _json_literal(r.status_code).encode(),
            b',"duration_ms":', _json_literal(r.duration_ms).encode(),
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

    def stream_jsonl():
        own_db = database.StreamSessionLocal()
        try:
            query = (
                own_db.query(RequestModel)
                .options(defer(RequestModel.response_body_raw))
                .filter(RequestModel.port_id == port_id)
            )
            if method_filter == "api":
                query = query.filter(RequestModel.method.in_(["POST", "PUT", "PATCH", "DELETE"]))
            query = query.order_by(RequestModel.created_at.asc())

            if _simple:
                # Flat array: [{"index":1,...}, ...]
                yield b"["
                first = True
                idx = 0
                for r in query.yield_per(100):
                    idx += 1
                    if not first:
                        yield b","
                    first = False
                    yield _build_simple_row(r, idx)
                yield b"]"
            else:
                # Full: {"port":{...},"total_requests":N,"requests":[...]}
                yield b'{"port":'
                yield json.dumps({
                    "port_number": _port_number,
                    "target_url": _port_target,
                    "description": _port_desc,
                    "created_at": _port_created,
                }, ensure_ascii=False).encode("utf-8")
                yield f',"total_requests":{total_count},"requests":['.encode("utf-8")

                first = True
                for r in query.yield_per(100):
                    if not first:
                        yield b","
                    first = False
                    yield _build_full_row(r)
                yield b"]}"
        finally:
            own_db.close()

    return StreamingResponse(
        stream_jsonl(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
