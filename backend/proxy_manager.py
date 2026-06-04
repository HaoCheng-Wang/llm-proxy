"""
Proxy Manager — manages multiple proxy server instances on different ports.
Each proxy server runs as an asyncio task using uvicorn.Server.
"""
import asyncio
import os
import sys
import tempfile
import uvicorn
from sqlalchemy.orm import Session
import database
from models import Port
from proxy_app import create_proxy_app, refresh_port_cache


# ---------------------------------------------------------------------------
# 跨平台文件锁 — 确保多 worker 模式下只有一个进程执行 restore
# 持有锁的进程崩溃时 OS 自动释放，不会死锁。
# ---------------------------------------------------------------------------
def _acquire_restore_lock() -> int | None:
    """Try to acquire an exclusive non-blocking file lock.
    Returns the fd (int) if acquired, None if another process holds it."""
    lock_path = os.path.join(tempfile.gettempdir(), "llm_proxy_restore.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None

    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            return lock_fd
        except OSError:
            os.close(lock_fd)
            return None
    else:
        import fcntl
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_fd
        except OSError:
            os.close(lock_fd)
            return None


def _release_restore_lock(lock_fd: int):
    """Release the restore lock and close the fd."""
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


async def _serve_proxy(server: uvicorn.Server, port_number: int):
    """Wrapper that prevents uvicorn's internal sys.exit(1) from killing
    the worker process when port binding fails."""
    try:
        await server.serve()
    except SystemExit as e:
        print(f"[ProxyManager] Proxy on port {port_number} stopped (exit code={e.code})")
    except asyncio.CancelledError:
        # Normal cancellation from stop_proxy, don't log
        raise
    except Exception as e:
        print(f"[ProxyManager] Proxy on port {port_number} error: {e}")


class ProxyManager:
    """Manages multiple uvicorn proxy servers on different ports."""

    def __init__(self):
        self._servers: dict[int, uvicorn.Server] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    async def start_proxy(self, port_number: int):
        """Start a proxy server on the given port."""
        if port_number in self._servers:
            await self.stop_proxy(port_number)

        # Check if port is already in use before starting
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", port_number))
            if result == 0:
                print(f"[ProxyManager] Port {port_number} is already in use, skipping")
                return
        finally:
            sock.close()

        app = create_proxy_app()
        config = uvicorn.Config(
            app=app,
            host="0.0.0.0",
            port=port_number,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._servers[port_number] = server

        # Run server as a background task (wrapped to prevent sys.exit(1) from
        # killing the worker process when port is already taken)
        task = asyncio.create_task(_serve_proxy(server, port_number))
        self._tasks[port_number] = task
        print(f"[ProxyManager] Proxy started on port {port_number}")

    async def stop_proxy(self, port_number: int):
        """Stop a proxy server on the given port."""
        server = self._servers.pop(port_number, None)
        task = self._tasks.pop(port_number, None)

        if server:
            server.should_exit = True
            if task:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            print(f"[ProxyManager] Proxy stopped on port {port_number}")

    async def stop_all(self):
        """Stop all running proxy servers."""
        ports = list(self._servers.keys())
        for port in ports:
            try:
                await self.stop_proxy(port)
            except (asyncio.CancelledError, Exception) as e:
                print(f"[ProxyManager] Warning: error stopping port {port}: {e}")
        print("[ProxyManager] All proxies stopped")

    def is_running(self, port_number: int) -> bool:
        return port_number in self._servers

    def get_active_ports(self) -> list[int]:
        return list(self._servers.keys())

    async def restore_from_database(self):
        """Restart all proxies that were active in the database.
        Uses a cross-platform file lock so only one worker process does the restore."""
        lock_fd = _acquire_restore_lock()
        if lock_fd is None:
            print("[ProxyManager] Another worker is handling restore, skipping")
            return

        try:
            db = database.SessionLocal()
            try:
                active_ports = db.query(Port).filter(Port.is_active.is_(True)).all()
                for port in active_ports:
                    if port.port_number not in self._servers:
                        try:
                            await self.start_proxy(port.port_number)
                        except OSError as e:
                            print(f"[ProxyManager] Failed to restore port {port.port_number}: {e}")
                refresh_port_cache(db)
            finally:
                db.close()
        finally:
            _release_restore_lock(lock_fd)
