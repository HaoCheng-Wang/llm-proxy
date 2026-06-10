"""
Proxy Manager — tracks proxy port configurations in the database.

In the shared-proxy architecture, there are no per-port servers to manage.
All proxy traffic flows through a single shared endpoint that routes by
port_number in the URL path. This module now only handles:
  1. Port cache refresh (port_number → (target_url, prefer_http2) mapping)
  2. Database state queries
"""
import logging
import database
from models import Port
from proxy_app import refresh_port_cache

logger = logging.getLogger("llm_proxy.proxy_manager")


class ProxyManager:
    """Manages proxy port configurations (DB-only, no server lifecycle)."""

    def get_active_ports(self) -> list[int]:
        """Get all active port numbers from database."""
        db = database.SessionLocal()
        try:
            ports = db.query(Port.port_number).filter(
                Port.is_active.is_(True), Port.deleted_at.is_(None)
            ).all()
            return [p[0] for p in ports]
        finally:
            db.close()

    def is_active(self, port_number: int) -> bool:
        """Check if a port is marked active in database."""
        db = database.SessionLocal()
        try:
            return db.query(Port).filter(
                Port.port_number == port_number,
                Port.is_active.is_(True),
                Port.deleted_at.is_(None),
            ).first() is not None
        finally:
            db.close()

    async def restore_from_database(self):
        """Refresh the port cache on startup."""
        refresh_port_cache()
        active_count = len(self.get_active_ports())
        logger.info("Loaded %d active proxy configurations", active_count)
