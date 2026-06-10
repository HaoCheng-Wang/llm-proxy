"""
LLM Proxy — Main Entry Point

Architecture: Shared Proxy — all traffic flows through a single endpoint.
  • Management API: /api/* (auth, port CRUD, history)
  • Proxy endpoint: /{port_number}/{path} (forwards to target)
  • Users only change base_url, no API keys or header changes needed
  • Example: http://server:3998/1/v1/chat/completions
    1 = logical port number identifying the proxy configuration
"""
import sys
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import init_database, setup_schema
import database
from models import User
from auth import hash_password
from config import (
    DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD,
    API_PORT, CORS_ORIGINS,
)
from proxy_manager import ProxyManager
from proxy_app import close_shared_client, close_http2_client, init_shared_client, init_http2_client


# Configure structured logging for the proxy module.
# Uses uvicorn's log format when running under uvicorn, or plain stream handler
# otherwise.  The llm_proxy.proxy logger is set to DEBUG so retry decisions
# and connection lifecycle events always appear in the log.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
logging.getLogger("llm_proxy.proxy").setLevel(logging.DEBUG)


# ---- Lifecycle ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("[Main] Initializing database...")
    init_database()

    print("[Main] Seeding default admin account...")
    seed_admin()

    print("[Main] Initializing proxy manager...")
    proxy_manager = ProxyManager()
    app.state.proxy_manager = proxy_manager

    print("[Main] Loading proxy configurations from database...")
    await proxy_manager.restore_from_database()

    # Pre-create httpx clients (HTTP/1.1 default, HTTP/2 opt-in per port).
    print("[Main] Initializing HTTP/1.1 client...")
    init_shared_client()
    print("[Main] Initializing HTTP/2 client...")
    init_http2_client()

    print(f"[Main] Management API + Shared Proxy ready on port {API_PORT}")
    print(f"[Main] Proxy URL format: http://<server>:{API_PORT}/<port_number>/v1/...")
    yield

    # Shutdown
    print("[Main] Closing HTTP/1.1 client...")
    await close_shared_client()
    print("[Main] Closing HTTP/2 client...")
    await close_http2_client()

    print("[Main] Disposing database connection pools...")
    if database.engine:
        database.engine.dispose()
    if database._log_engine:
        database._log_engine.dispose()

    print("[Main] Shutting down DB executor thread pool...")
    database.shutdown_db_executor()

    print("[Main] Shutdown complete.")


def seed_admin():
    """Create default admin account if it doesn't exist."""
    db = database.SessionLocal()
    try:
        admin = db.query(User).filter(User.username == DEFAULT_ADMIN_USERNAME).first()
        if not admin:
            admin = User(
                username=DEFAULT_ADMIN_USERNAME,
                password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
                role="admin",
                is_approved=True,
            )
            db.add(admin)
            db.commit()
            print(f"  Created admin user: {DEFAULT_ADMIN_USERNAME}")

        # Warn if using default password
        if DEFAULT_ADMIN_PASSWORD == "admin123":
            print()
            print("=" * 60)
            print("  [WARNING] Admin password is still the default 'admin123'")
            print("  [WARNING] Change DEFAULT_ADMIN_PASSWORD in .env immediately!")
            print("=" * 60)
            print()
    finally:
        db.close()


# ---- FastAPI App ----
app = FastAPI(
    title="LLM Proxy",
    description="Intercept and record communication between AI agents and LLM APIs",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow configured origins (defaults to localhost for dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
from routers.auth_router import router as auth_router      # noqa: E402
from routers.admin_router import router as admin_router    # noqa: E402
from routers.ports_router import router as ports_router    # noqa: E402
from routers.config_router import router as config_router  # noqa: E402

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(ports_router)
app.include_router(config_router)

# Shared proxy endpoint — routes all /{port_number}/{path} traffic.
# This MUST be registered after /api/ routes so that /api/* paths
# are matched first (static routes take priority over parameterized).
from shared_proxy import router as shared_proxy_router  # noqa: E402
app.include_router(shared_proxy_router)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "llm-proxy"}


# ---- Run ----
if __name__ == "__main__":
    import uvicorn

    # Run DDL once before starting server
    print("[Main] Running schema setup...")
    setup_schema()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=API_PORT,
        reload=False,
    )
