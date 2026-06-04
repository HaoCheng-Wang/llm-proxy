"""
LLM Proxy — Main Entry Point

Starts:
  1. FastAPI management API (multi-worker for concurrent users)
  2. Proxy server manager that restores previously active proxy ports

Usage:
  python main.py
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import init_database
import database
from models import User
from auth import hash_password
from config import (
    DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD,
    API_PORT, API_WORKERS, CORS_ORIGINS,
)
from proxy_manager import ProxyManager
from proxy_app import close_shared_client


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

    print("[Main] Restoring active proxies from database...")
    await proxy_manager.restore_from_database()

    print(f"[Main] Management API ready on port {API_PORT}")
    yield

    # Shutdown
    print("[Main] Shutting down all proxies...")
    try:
        await proxy_manager.stop_all()
    except Exception as e:
        print(f"[Main] Warning during proxy shutdown: {e}")

    print("[Main] Closing shared HTTP client...")
    await close_shared_client()

    print("[Main] Disposing database connection pool...")
    if database.engine:
        database.engine.dispose()

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
            print("  ⚠️  WARNING: Admin password is still the default 'admin123'")
            print("  ⚠️  Change DEFAULT_ADMIN_PASSWORD in .env immediately!")
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


@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "llm-proxy"}


# ---- Run ----
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=API_PORT,
        workers=API_WORKERS,
        reload=False,
    )
