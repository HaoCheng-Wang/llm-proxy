"""
Shared fixtures for LLM Proxy tests.
Provides test client, auth tokens, and database setup.
"""
import os
import sys
import asyncio
import pytest
import pytest_asyncio

# Add backend to path so we can import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# Override env vars BEFORE importing any backend modules
os.environ["DATABASE_NAME"] = "llm_proxy_test"
os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["DEFAULT_ADMIN_USERNAME"] = "admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "admin123"
os.environ["API_PORT"] = "19998"
os.environ["ALLOW_INTERNAL_TARGETS"] = "true"

from httpx import AsyncClient, ASGITransport
from main import app
import database
from models import Base
from auth import hash_password, create_access_token
from models import User, Port
from proxy_manager import ProxyManager


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def setup_database():
    """Initialize test database, create tables, and set up app state."""
    database._ensure_database()
    database.init_database()
    Base.metadata.create_all(bind=database.engine)

    # Seed admin user
    db = database.SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            admin = User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="admin",
                is_approved=True,
            )
            db.add(admin)
            db.commit()
    finally:
        db.close()

    # Set proxy_manager on app.state (normally done by lifespan)
    if not hasattr(app.state, "proxy_manager") or app.state.proxy_manager is None:
        app.state.proxy_manager = ProxyManager()

    yield

    # Cleanup — only drop tables in the TEST database.
    # Guard: refuse to drop if we somehow connected to production.
    _db_name = getattr(database.engine.url, 'database', '')
    if 'test' not in _db_name.lower():
        raise RuntimeError(
            f"SAFETY: refusing to drop_all() on non-test database '{_db_name}'. "
            f"Expected a database name containing 'test'."
        )
    if database.engine:
        Base.metadata.drop_all(bind=database.engine)
        database.engine.dispose()


@pytest_asyncio.fixture
async def client(setup_database):
    """Create an async test client for the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def admin_user(setup_database):
    """Get the admin user from DB."""
    db = database.SessionLocal()
    try:
        user = db.query(User).filter(User.username == "admin").first()
        yield user
    finally:
        db.close()


@pytest_asyncio.fixture
async def approved_user(setup_database):
    """Create an approved test user in the database. Resets password each time."""
    db = database.SessionLocal()
    try:
        user = db.query(User).filter(User.username == "testuser").first()
        if not user:
            user = User(
                username="testuser",
                password_hash=hash_password("testpass123"),
                role="user",
                is_approved=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            # Reset password in case a test changed it
            user.password_hash = hash_password("testpass123")
            user.is_approved = True
            db.commit()
        yield user
    finally:
        db.close()


@pytest_asyncio.fixture
async def unapproved_user(setup_database):
    """Create an unapproved test user in the database."""
    db = database.SessionLocal()
    try:
        user = db.query(User).filter(User.username == "pendinguser").first()
        if not user:
            user = User(
                username="pendinguser",
                password_hash=hash_password("pendingpass"),
                role="user",
                is_approved=False,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            user.is_approved = False
            user.password_hash = hash_password("pendingpass")
            db.commit()
        yield user
    finally:
        db.close()


@pytest.fixture
def admin_headers(admin_user):
    """Authorization headers for admin user."""
    token = create_access_token({"user_id": admin_user.id, "role": "admin"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user_headers(approved_user):
    """Authorization headers for regular approved user."""
    token = create_access_token({"user_id": approved_user.id, "role": "user"})
    return {"Authorization": f"Bearer {token}"}
