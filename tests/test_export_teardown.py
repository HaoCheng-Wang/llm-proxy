"""Teardown tests for the streaming export path.

The export's only zombie protection is force-dropping the raw MySQL socket
before invalidating the connection: PyMySQL otherwise spins on the socket
until it has read every remaining packet of the server-side result set
(`_finish_unbuffered_query`), and any later command — including the ROLLBACK
that `Session.close()` issues — triggers that drain.
"""
import os
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import database
from routers import ports_router


def _raw_socket(session):
    dbapi = session.connection().connection
    return getattr(dbapi, "driver_connection", dbapi)


class TestForceDrop:
    def test_destroys_the_raw_socket(self, setup_database):
        """Regression guard: this used to be a silent no-op.

        It read `session.connection` (the attribute) instead of calling it, so
        `conn.connection` raised AttributeError into a bare `except: pass` and
        the force-drop never ran — leaving the drain free to execute.
        """
        session = database.StreamSessionLocal()
        try:
            session.execute(text("SELECT 1"))
            raw = _raw_socket(session)
            assert raw._sock is not None, "precondition: live socket"

            ports_router._drop_export_connection(session, 999999)

            assert raw._sock is None, "raw socket was not destroyed"
        finally:
            try:
                session.invalidate()
            except Exception:
                pass

    def test_is_safe_without_a_live_connection(self, setup_database):
        """Called defensively from a finally block — must never raise."""
        session = database.StreamSessionLocal()
        session.invalidate()          # no live DBAPI connection to drop
        # Must not raise even though there is nothing to force-close.
        ports_router._drop_export_connection(session, 999999)

    def test_dropped_connection_cannot_drain_unread_rows(self, setup_database):
        """With the socket gone, a pending teardown fails fast instead of
        reading the rest of the result set."""
        session = database.StreamSessionLocal()
        try:
            session.execute(text("DROP TABLE IF EXISTS _drain_probe"))
            session.execute(text("CREATE TABLE _drain_probe (id INT)"))
            session.execute(text(
                "INSERT INTO _drain_probe VALUES (1), (2), (3), (4), (5)"))
            session.commit()

            rows = session.execute(text("SELECT * FROM _drain_probe"))
            assert rows.fetchone() is not None      # rows 2-5 left unread
            raw = _raw_socket(session)
            assert raw._result is not None
            assert raw._result.unbuffered_active is True, (
                "precondition: a server-side result set is still open")

            ports_router._drop_export_connection(session, 999999)

            # The drain must be disabled outright: socket gone AND result
            # marked inactive, so PyMySQL's spin loop never runs.  (Either
            # alone is sufficient — both together means no teardown path can
            # resurrect it, whichever order they run in.)
            assert raw._sock is None, "socket still alive"
            assert raw._result.unbuffered_active is False, (
                "result left active — a later close/rollback would drain it"
            )
            # Must return without touching the network.
            raw._result._finish_unbuffered_query()
        finally:
            try:
                session.invalidate()
            except Exception:
                pass
            cleanup = database.StreamSessionLocal()
            try:
                cleanup.execute(text("DROP TABLE IF EXISTS _drain_probe"))
                cleanup.commit()
            except Exception:
                pass
            finally:
                cleanup.close()


class _SpySession:
    """Wraps a real session and records which teardown it received."""

    def __init__(self, real):
        self._real = real
        self.closed = 0
        self.invalidated = 0

    def close(self):
        self.closed += 1
        self._real.close()

    def invalidate(self):
        self.invalidated += 1
        self._real.invalidate()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FailingQuerySpy(_SpySession):
    """Fails before iteration starts, to exercise the early-exit teardown."""

    def query(self, *a, **k):
        raise RuntimeError("boom before iteration")


@pytest.fixture
def spy_sessions(monkeypatch):
    """Replace StreamSessionLocal with a recording factory."""
    made = []
    real_factory = database.StreamSessionLocal

    def factory(cls=None):
        session = (cls or _SpySession)(real_factory())
        made.append(session)
        return session

    monkeypatch.setattr(database, "StreamSessionLocal", factory)
    return made


async def _make_port(client, admin_headers, desc):
    resp = await client.post("/api/ports", headers=admin_headers, json={
        "target_url": "https://httpbin.org", "description": desc,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestTeardownPolicy:
    async def test_successful_export_closes_cleanly(
            self, client, admin_headers, spy_sessions):
        """A fully-drained result set returns its connection to the pool."""
        port_id = await _make_port(client, admin_headers, "export close test")

        resp = await client.get(f"/api/ports/{port_id}/export",
                               headers=admin_headers)
        assert resp.status_code == 200

        assert spy_sessions, "export never opened a stream session"
        s = spy_sessions[-1]
        assert s.closed == 1, "clean export should close() the session"
        assert s.invalidated == 0, "clean export must not discard the connection"

    async def test_early_exit_invalidates_instead_of_closing(
            self, client, admin_headers, monkeypatch, spy_sessions):
        """Any non-clean exit must discard the connection, never close() it.

        close() issues a ROLLBACK, and PyMySQL answers a command on a
        connection with a live server-side cursor by draining every unread row.
        """
        port_id = await _make_port(client, admin_headers, "export abort test")
        original = database.StreamSessionLocal

        def factory():
            return _FailingQuerySpy(original())

        monkeypatch.setattr(database, "StreamSessionLocal", factory)

        with pytest.raises(Exception):
            await client.get(f"/api/ports/{port_id}/export", headers=admin_headers)

        assert spy_sessions, "export never opened a stream session"
        s = spy_sessions[-1]
        assert s.invalidated == 1, "aborted export must invalidate()"
        assert s.closed == 0, "aborted export must not close()"
