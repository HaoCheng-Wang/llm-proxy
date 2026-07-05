from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Index
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="user")
    is_approved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    ports = relationship("Port", back_populates="user", cascade="all, delete-orphan")


class Port(Base):
    __tablename__ = "ports"

    id = Column(Integer, primary_key=True, index=True)
    port_number = Column(Integer, unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    target_url = Column(String(500), nullable=False)
    description = Column(String(200), default="")
    is_active = Column(Boolean, default=True)
    prefer_http2 = Column(Boolean, nullable=True)  # NULL=HTTP/1.1, False=HTTP/1.1, True=HTTP/2
    api_key = Column(String(500), nullable=True)   # NULL=pass-through, set=override agent's key
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    user = relationship("User", back_populates="ports")
    requests = relationship("Request", back_populates="port", cascade="all, delete-orphan",
                            order_by="Request.created_at")


class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (
        # Composite index for fast filtering by port_id, method, and ordering by created_at.
        # Without this, COUNT/ORDER BY on a port's requests requires a filesort.
        Index("ix_requests_port_method_created", "port_id", "method", "created_at"),
        {"mysql_charset": "utf8mb4"},
    )

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=True, index=True)
    method = Column(String(10), nullable=False)
    path = Column(String(1000), nullable=False)
    request_headers = Column(LONGTEXT, nullable=True)
    request_body = Column(LONGTEXT, nullable=True)
    response_headers = Column(LONGTEXT, nullable=True)
    response_body = Column(LONGTEXT, nullable=True)
    response_body_raw = Column(LONGTEXT, nullable=True)
    status_code = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    reconstruction_error = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True)  # Added index

    port = relationship("Port", back_populates="requests")


