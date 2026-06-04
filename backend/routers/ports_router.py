import json
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import User, Port, Request as RequestModel
from schemas import PortCreate, PortInfo, PortHistory, RequestInfo
from auth import get_current_user, require_approved
from config import PROXY_PORT_START, PROXY_PORT_END, MAX_PORTS_PER_USER, ALLOW_INTERNAL_TARGETS
from proxy_manager import ProxyManager, refresh_port_cache

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


def get_proxy_manager(request: Request) -> ProxyManager:
    """Get the ProxyManager instance from the app state."""
    return request.app.state.proxy_manager


@router.post("", response_model=PortInfo)
async def create_port(
    data: PortCreate,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
    proxy_manager: ProxyManager = Depends(get_proxy_manager),
):
    """Create a new proxy port for the current user."""
    # Check port limit per user
    user_port_count = db.query(Port).filter(
        Port.user_id == current_user.id,
        Port.is_active.is_(True)
    ).count()
    if user_port_count >= MAX_PORTS_PER_USER:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_PORTS_PER_USER} active ports per user")

    # Find an available port
    used_ports = {p[0] for p in db.query(Port.port_number).filter(Port.is_active.is_(True)).all()}
    used_ports.update(proxy_manager.get_active_ports())

    assigned_port = None
    for port_num in range(PROXY_PORT_START, PROXY_PORT_END + 1):
        if port_num not in used_ports:
            assigned_port = port_num
            break

    if assigned_port is None:
        raise HTTPException(status_code=503, detail="No available ports in range 4000-5000")

    # Strip trailing slash from target_url
    target_url = data.target_url.rstrip("/")

    # Validate target URL (SSRF protection)
    _validate_target_url(target_url)

    # Create port record in DB
    port = Port(
        port_number=assigned_port,
        user_id=current_user.id,
        target_url=target_url,
        description=data.description,
        is_active=True,
    )
    db.add(port)
    db.commit()
    db.refresh(port)

    # Start the proxy server
    try:
        await proxy_manager.start_proxy(assigned_port)
        refresh_port_cache(db)
    except OSError as e:
        # If port fails to start, still keep the record but mark inactive
        port.is_active = False
        db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to start proxy on port {assigned_port}: {e}")

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
        ports = db.query(Port).order_by(Port.created_at.desc()).all()
    else:
        ports = db.query(Port).filter(Port.user_id == current_user.id).order_by(Port.created_at.desc()).all()

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
    proxy_manager: ProxyManager = Depends(get_proxy_manager),
    db: Session = Depends(get_db),
):
    """Get all active port numbers (admin sees all, users see theirs)."""
    if current_user.role == "admin":
        return proxy_manager.get_active_ports()
    else:
        user_ports = db.query(Port.port_number).filter(
            Port.user_id == current_user.id,
            Port.is_active.is_(True)
        ).all()
        return [p[0] for p in user_ports if proxy_manager.is_running(p[0])]


@router.get("/{port_id}", response_model=PortHistory)
def get_port_history(
    port_id: int,
    since_id: int = 0,
    limit: int = 20,
    offset: int = 0,
    load_all: bool = False,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Get detailed history for a specific port.
    If since_id > 0, only return records with id > since_id (for incremental polling).
    Supports pagination via limit (default 20, max 100) and offset.
    If load_all is true, returns all records (bypasses limit cap)."""
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
    if load_all:
        requests = query.order_by(RequestModel.created_at.desc()).all()
    else:
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
                created_at=r.created_at,
            ) for r in requests
        ]
    )


@router.delete("/{port_id}", response_model=dict)
async def delete_port(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
    proxy_manager: ProxyManager = Depends(get_proxy_manager),
):
    """Stop and delete a proxy port."""
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Stop the proxy server
    if proxy_manager.is_running(port.port_number):
        await proxy_manager.stop_proxy(port.port_number)

    # Delete port and its request history
    db.delete(port)
    db.commit()
    refresh_port_cache(db)

    return {"message": f"Port {port.port_number} has been deleted."}


@router.post("/{port_id}/stop", response_model=dict)
async def stop_port(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
    proxy_manager: ProxyManager = Depends(get_proxy_manager),
):
    """Stop a proxy port without deleting it. Owner or admin only."""
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if not port.is_active:
        raise HTTPException(status_code=400, detail="Port is already stopped")

    # Stop the proxy server
    if proxy_manager.is_running(port.port_number):
        await proxy_manager.stop_proxy(port.port_number)

    port.is_active = False
    db.commit()
    refresh_port_cache(db)

    return {"message": f"Port {port.port_number} has been stopped."}


@router.post("/{port_id}/start", response_model=dict)
async def start_port(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
    proxy_manager: ProxyManager = Depends(get_proxy_manager),
):
    """Start a previously stopped proxy port. Owner or admin only."""
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if port.is_active:
        raise HTTPException(status_code=400, detail="Port is already running")

    # Check port limit
    user_port_count = db.query(Port).filter(
        Port.user_id == port.user_id,
        Port.is_active.is_(True)
    ).count()
    if user_port_count >= MAX_PORTS_PER_USER:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_PORTS_PER_USER} active ports per user")

    # Start the proxy server
    try:
        await proxy_manager.start_proxy(port.port_number)
        port.is_active = True
        db.commit()
        refresh_port_cache(db)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to start proxy on port {port.port_number}: {e}")

    return {"message": f"Port {port.port_number} has been started."}


@router.delete("/{port_id}/history", response_model=dict)
def clear_port_history(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Clear all request history for a port, but keep the port active."""
    port = db.query(Port).filter(Port.id == port_id).first()
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

    db.delete(req_record)
    db.commit()
    return {"message": "Request record deleted."}


@router.get("/{port_id}/export", response_model=dict)
def export_port_history(
    port_id: int,
    current_user: User = Depends(require_approved),
    db: Session = Depends(get_db),
):
    """Export all request history for a port as a JSON-serializable structure."""
    port = db.query(Port).filter(Port.id == port_id).first()
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")

    if current_user.role != "admin" and port.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    requests = db.query(RequestModel).filter(
        RequestModel.port_id == port.id
    ).order_by(RequestModel.created_at.asc()).all()

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
