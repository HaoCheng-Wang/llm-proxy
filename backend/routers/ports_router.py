import ipaddress
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from database import get_db
from models import User, Port, Request as RequestModel
from schemas import PortCreate, PortUpdate, PortInfo, PortHistory, RequestInfo
from auth import require_approved
from config import ALLOW_INTERNAL_TARGETS
from proxy_app import refresh_port_cache

router = APIRouter(prefix="/api/ports", tags=["ports"])


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


@router.get("/{port_id}", response_model=PortHistory)
def get_port_history(
    port_id: int,
    since_id: int = 0,
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Get detailed history for a specific port.
    If since_id > 0, only return records with id > since_id (for incremental polling).
    Supports pagination via limit (default 20, max 100) and offset."""
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    # Check ownership (admin can view all)
    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    request_count = db.query(func.count(RequestModel.id)).filter(
        RequestModel.port_id == port.id
    ).scalar() or 0

    query = db.query(RequestModel).filter(
        RequestModel.port_id == port.id
    )
    if since_id > 0:
        query = query.filter(RequestModel.id > since_id)
    # Cap limit to prevent huge responses
    limit = min(max(limit, 1), 100)
    requests = query.order_by(RequestModel.created_at.desc()).offset(offset).limit(limit).all()

    # Get creator username
    creator = db.query(User).filter(User.id == port.user_id).first()
    creator_name = creator.username if creator else ""

    return PortHistory(
        port=PortInfo(
            id=port.id,
            port_number=port.port_number,
            target_url=port.target_url,
            description=port.description or "",
            is_active=port.is_active,
            deleted_at=port.deleted_at,
            created_at=port.created_at,
            request_count=request_count,
            username=creator_name,
        ),
        requests=[
            RequestInfo(
                id=r.id,
                port_id=r.port_id,
                method=r.method,
                path=r.path,
                request_headers=r.request_headers,
                request_body=r.request_body,
                response_headers=r.response_headers,
                response_body=r.response_body,
                response_body_raw=r.response_body_raw,
                status_code=r.status_code,
                duration_ms=r.duration_ms,
                reconstruction_error=r.reconstruction_error,
                created_at=r.created_at,
            ) for r in requests
        ]
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


@router.get("/{port_id}/export", response_model=dict)
def export_port_history(
    port_id: int,
    method_filter: str = "all",
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Export all request history for a port as a JSON-serializable structure.
    
    Args:
        method_filter: 'all' (default) or 'api' (POST/PUT/PATCH/DELETE only).
    """
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    requests = db.query(RequestModel).filter(
        RequestModel.port_id == port.id
    ).order_by(RequestModel.created_at.asc()).all()

    # Apply method filter
    if method_filter == "api":
        api_methods = {"POST", "PUT", "PATCH", "DELETE"}
        requests = [r for r in requests if r.method.upper() in api_methods]

    export_data = {
        "port": {
            "port_number": port.port_number,
            "target_url": port.target_url,
            "description": port.description,
            "created_at": port.created_at.isoformat() if port.created_at else None,
        },
        "total_requests": len(requests),
        "requests": []
    }

    for r in requests:
        req_entry = {
            "id": r.id,
            "method": r.method,
            "path": r.path,
            "status_code": r.status_code,
            "duration_ms": r.duration_ms,
            "timestamp": r.created_at.isoformat() if r.created_at else None,
        }
        # Parse JSON strings back to objects for cleaner export
        for field in ["request_headers", "request_body", "response_headers", "response_body"]:
            raw = getattr(r, field, None)
            if raw:
                try:
                    req_entry[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    req_entry[field] = raw
            else:
                req_entry[field] = None
        export_data["requests"].append(req_entry)

    return export_data
