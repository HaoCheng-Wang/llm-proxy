from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Boolean, ForeignKey, Index
from sqlalchemy.dialects.mysql import LONGTEXT, LONGBLOB
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    user = relationship("User", back_populates="ports")
    requests = relationship("Request", back_populates="port", cascade="all, delete-orphan",
                            order_by="Request.created_at")


class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (
        # Composite index for fast filtering by port_id and ordering by created_at
        # This speeds up the get_port_history query significantly
        {"mysql_charset": "utf8mb4"},
    )

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=False, index=True)  # Added index
    method = Column(String(10), nullable=False)
    path = Column(String(1000), nullable=False)
    request_headers = Column(LONGTEXT, nullable=True)
    request_body = Column(LONGTEXT, nullable=True)
    response_headers = Column(LONGTEXT, nullable=True)
    response_body = Column(LONGTEXT, nullable=True)
    response_body_raw = Column(LONGTEXT, nullable=True)
    status_code = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True)  # Added index

    port = relationship("Port", back_populates="requests")


# ──────────────────────────────────────────────
#  Stream write-ahead logging — chunk-at-a-time
# ──────────────────────────────────────────────

class StreamSession(Base):
    """Metadata for a streaming SSE proxy request.

    Created before the first chunk is forwarded.  The proxy process writes
    nothing to memory/disk — only to this row + stream_chunks.
    A background worker later reads all chunks, reconstructs the full JSON,
    and creates the final ``Request`` record.
    """
    __tablename__ = "stream_sessions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    stream_id = Column(String(32), unique=True, nullable=False, index=True)
    port_number = Column(Integer, nullable=False, index=True)
    method = Column(String(10), nullable=False)
    path = Column(String(1000), nullable=False)
    request_headers = Column(LONGTEXT, nullable=True)
    request_body = Column(LONGTEXT, nullable=True)
    response_headers = Column(LONGTEXT, nullable=True)
    status_code = Column(Integer, nullable=True)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    duration_ms = Column(Integer, nullable=True)
    is_complete = Column(Boolean, default=False, index=True)
    is_processed = Column(Boolean, default=False)
    error_message = Column(String(500), nullable=True)


class StreamChunk(Base):
    """One raw SSE chunk belonging to a stream session.

    Written fire-and-forget during streaming; read back in seq order
    by the reconstruction worker.
    """
    __tablename__ = "stream_chunks"
    __table_args__ = (
        Index("ix_stream_chunks_stream_seq", "stream_id", "seq"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    stream_id = Column(String(32), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    chunk_data = Column(LONGBLOB, nullable=False)
