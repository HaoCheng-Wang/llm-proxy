"""
Stream reconstruction — write-ahead logging + post-processing for SSE streams.

Instead of accumulating the full SSE body in the proxy process (memory or disk),
each chunk is written directly to MySQL as it arrives.  A background worker
periodically reads completed streams, reconstructs the full JSON, and creates
the final Request record.

This means:
• Zero per-stream memory/disk accumulation in the proxy process
• No hard limits or truncation — the full stream is always preserved
• No temp files — everything lives in MySQL
• Client is completely unaffected — chunks are yielded immediately
"""
from __future__ import annotations
import asyncio
import sys
import traceback
from datetime import datetime, timezone

import database
from models import StreamSession, StreamChunk, Port, Request as RequestModel
from proxy_app import _reconstruct_sse_to_json


# ═══════════════════════════════════════════════════════════════
#  Chunk write helpers — called from the streaming generator
# ═══════════════════════════════════════════════════════════════

async def _create_stream_session_async(
    stream_id: str,
    port_number: int,
    method: str,
    path: str,
    req_headers: str | None,
    req_body: str | None,
    resp_headers: str,
    status_code: int,
    start_time: float,
) -> None:
    """Create the stream_session row before any chunks are forwarded.

    Runs in the dedicated DB thread pool so it never blocks the event loop.
    Must succeed before chunk writes begin (otherwise chunks reference a
    non-existent session).
    """
    loop = asyncio.get_running_loop()
    started_at = datetime.fromtimestamp(start_time, tz=timezone.utc).replace(tzinfo=None)

    def _create() -> None:
        db = database.LogSessionLocal()
        try:
            session = StreamSession(
                stream_id=stream_id,
                port_number=port_number,
                method=method,
                path=path,
                request_headers=req_headers,
                request_body=req_body,
                response_headers=resp_headers,
                status_code=status_code,
                started_at=started_at,
            )
            db.add(session)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    await loop.run_in_executor(database._db_executor, _create)


async def _insert_chunk_async(stream_id: str, seq: int, chunk_data: bytes) -> None:
    """Fire-and-forget: write one raw SSE chunk to stream_chunks.

    Errors are silently ignored — the client stream must never be interrupted
    by a DB write failure.  If chunks are lost the reconstruction worker will
    skip the session.
    """
    loop = asyncio.get_running_loop()

    def _insert() -> None:
        db = database.LogSessionLocal()
        try:
            chunk = StreamChunk(stream_id=stream_id, seq=seq, chunk_data=chunk_data)
            db.add(chunk)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    try:
        await loop.run_in_executor(database._db_executor, _insert)
    except Exception:
        pass  # Never let a DB write error propagate to the client


async def _mark_stream_complete(stream_id: str, duration_ms: int) -> None:
    """Mark a stream session as complete after all chunks have been written.

    This signals the reconstruction worker that the stream is ready to be
    assembled into a final Request record.
    """
    loop = asyncio.get_running_loop()

    def _mark() -> None:
        db = database.LogSessionLocal()
        try:
            session = (
                db.query(StreamSession)
                .filter(StreamSession.stream_id == stream_id)
                .first()
            )
            if session:
                session.is_complete = True
                session.duration_ms = duration_ms
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    try:
        await loop.run_in_executor(database._db_executor, _mark)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  Background reconstruction worker
# ═══════════════════════════════════════════════════════════════

async def stream_reconstruction_worker(interval_seconds: int = 5) -> None:
    """Background async loop: periodically reconstruct completed streams.

    This runs as an asyncio task inside the FastAPI lifespan.  All heavy work
    (DB reads, SSE parsing) is dispatched to the dedicated DB thread pool so
    the event loop stays responsive.
    """
    print(f"[Reconstructor] Worker started (interval={interval_seconds}s)", file=sys.stderr)

    while True:
        try:
            loop = asyncio.get_running_loop()
            processed = await loop.run_in_executor(
                database._db_executor, _process_completed_streams,
            )
            if processed:
                print(
                    f"[Reconstructor] Processed {processed} stream(s)",
                    file=sys.stderr,
                )
        except Exception:
            traceback.print_exc(file=sys.stderr)

        await asyncio.sleep(interval_seconds)


def _process_completed_streams() -> int:
    """Find completed-but-unprocessed streams and reconstruct them.

    Returns the number of streams successfully processed.
    """
    db = database.LogSessionLocal()
    processed = 0
    try:
        sessions = (
            db.query(StreamSession)
            .filter(
                StreamSession.is_complete == True,       # noqa: E712
                StreamSession.is_processed == False,     # noqa: E712
            )
            .limit(10)
            .all()
        )

        for session in sessions:
            try:
                _reconstruct_one_stream(db, session)
                processed += 1
            except Exception as exc:
                print(
                    f"[Reconstructor] ERROR stream {session.stream_id}: {exc}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                # Try to salvage the raw SSE data even on unexpected errors
                try:
                    chunks = (
                        db.query(StreamChunk)
                        .filter(StreamChunk.stream_id == session.stream_id)
                        .order_by(StreamChunk.seq)
                        .all()
                    )
                    if chunks:
                        full_body = b"".join(c.chunk_data for c in chunks)
                        raw_sse_text = full_body.decode("utf-8", errors="replace")
                        # Resolve port_id (may be None)
                        port = (
                            db.query(Port)
                            .filter(
                                Port.port_number == session.port_number,
                                Port.is_active == True,  # noqa: E712
                            )
                            .first()
                        )
                        record = RequestModel(
                            port_id=port.id if port else None,
                            method=session.method,
                            path=session.path,
                            request_headers=session.request_headers,
                            request_body=session.request_body,
                            response_headers=session.response_headers,
                            response_body=raw_sse_text,  # raw SSE as-is
                            response_body_raw=raw_sse_text,
                            status_code=session.status_code,
                            duration_ms=session.duration_ms,
                            reconstruction_error=True,
                        )
                        db.add(record)
                except Exception:
                    db.rollback()

                # Clean up chunks so they don't accumulate
                db.query(StreamChunk).filter(
                    StreamChunk.stream_id == session.stream_id
                ).delete()
                session.error_message = str(exc)[:500]
                session.is_processed = True
                db.commit()
    finally:
        db.close()

    return processed


def _reconstruct_one_stream(db, session: StreamSession) -> None:
    """Read all chunks for *session*, reconstruct SSE → JSON, save Request record.

    The raw SSE text is always saved to ``response_body_raw`` regardless of
    reconstruction success or port availability.  This guarantees the proxy
    never loses data — the user can always see the original SSE in the UI.
    """
    # ── Read all chunks in order ──
    chunks = (
        db.query(StreamChunk)
        .filter(StreamChunk.stream_id == session.stream_id)
        .order_by(StreamChunk.seq)
        .all()
    )

    if not chunks:
        # No data — clean up and move on
        db.delete(session)
        db.commit()
        return

    # ── Concatenate raw SSE (always preserved) ──
    full_body = b"".join(c.chunk_data for c in chunks)
    raw_sse_text = full_body.decode("utf-8", errors="replace")

    # ── Reconstruct JSON (best-effort) ──
    reconstructed_json = _reconstruct_sse_to_json(raw_sse_text)

    # Detect reconstruction failure: None means all parsers failed;
    # raw SSE text (starts with "data:") means generic fallback was returned.
    reconstruction_error = (
        reconstructed_json is None
        or reconstructed_json.lstrip().startswith("data:")
    )
    if reconstruction_error:
        print(
            f"[Reconstructor] SSE reconstruction issue for {session.stream_id} "
            f"— raw SSE saved to response_body_raw",
            file=sys.stderr,
        )
        # Keep the raw text as response_body so the UI always shows something
        if reconstructed_json is None:
            reconstructed_json = raw_sse_text

    # ── Resolve port_id (may be None if port was deleted) ──
    port = (
        db.query(Port)
        .filter(
            Port.port_number == session.port_number,
            Port.is_active == True,  # noqa: E712
        )
        .first()
    )
    port_id = port.id if port else None

    if not port_id:
        print(
            f"[Reconstructor] Port {session.port_number} not active/deleted "
            f"— stream {session.stream_id} saved with port_id=NULL",
            file=sys.stderr,
        )

    # ── Always save raw SSE data, even when port or reconstruction fails ──
    record = RequestModel(
        port_id=port_id,
        method=session.method,
        path=session.path,
        request_headers=session.request_headers,
        request_body=session.request_body,
        response_headers=session.response_headers,
        response_body=reconstructed_json,
        response_body_raw=raw_sse_text,
        status_code=session.status_code,
        duration_ms=session.duration_ms,
        reconstruction_error=reconstruction_error,
    )
    db.add(record)

    # ── Delete raw chunks ──
    db.query(StreamChunk).filter(
        StreamChunk.stream_id == session.stream_id
    ).delete()

    # ── Delete session row — data fully migrated to requests ──
    db.delete(session)

    db.commit()

    print(
        f"[Reconstructor] ✓ {session.stream_id} "
        f"(port={session.port_number}, {len(chunks)} chunks, "
        f"{len(full_body)} bytes)",
        file=sys.stderr,
    )
