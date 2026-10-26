"""Run the Apogee server (internal package name: aims): localhost only, one instance per data directory.

Usage: python -m aims.server   (normally started by launcher.pyw)
Exit codes: 0 normal shutdown, 1 startup failure, 3 another instance already running.
"""

from __future__ import annotations

import logging
import msvcrt
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import uvicorn

from aims.config import load_config

EXIT_FAILED = 1
EXIT_ALREADY_RUNNING = 3


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive OS lock for the server's lifetime.

    Windows drops the lock when the process exits, even after a crash, so a leftover
    lock file never blocks the next start.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise AlreadyRunning(f"Another Apogee server is already using {path.parent}") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def main() -> int:
    config = load_config()
    config.ensure_dirs()
    log_path = config.log_dir / "server.log"
    # Under pythonw there is no console; keep uvicorn's stream logging from crashing.
    if sys.stdout is None or sys.stderr is None:
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("aims.server")

    try:
        with single_instance_lock(config.lock_path):
            from aims.web.app import create_app

            app = create_app(config)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.port, log_level="info"))
            app.state.server = server
            log.info("Starting Apogee on %s (data: %s)", config.base_url, config.data_dir)
            server.run()
            # A port-bind failure usually ends in SystemExit(1); guard against a quiet return too.
            return 0 if server.started else EXIT_FAILED
    except AlreadyRunning as exc:
        log.warning("%s", exc)
        return EXIT_ALREADY_RUNNING
    except Exception:
        log.exception("Apogee server failed to start")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
