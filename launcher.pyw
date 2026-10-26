"""Desktop-shortcut entry point (run with .venv\\Scripts\\pythonw.exe, so there is no console).

If an Apogee server is already running, open the browser to it. Otherwise start one in the
background, wait until it is healthy, then open the browser. Any failure is shown in a
message box together with the end of the server log.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from aims import APP_ID  # noqa: E402
from aims.config import load_config  # noqa: E402

TITLE = "Apogee"
STARTUP_TIMEOUT_S = 30
MAX_LOG_BYTES = 2_000_000
EXIT_ALREADY_RUNNING = 3  # mirrors aims.server; not imported to keep uvicorn out of the launcher

# Ignore any system proxy: we only ever talk to 127.0.0.1.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe(base_url: str) -> str:
    """Return 'ours', 'other' (something else owns the port), or 'none'."""
    try:
        resp = _opener.open(base_url + "/api/health", timeout=1.5)
    except urllib.error.HTTPError:
        return "other"
    except OSError:
        return "none"
    try:
        with resp:
            data = json.load(resp)
    except (ValueError, OSError):
        return "other"
    return "ours" if isinstance(data, dict) and data.get("app") == APP_ID else "other"


def error_box(message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
    except Exception:
        print(message, file=sys.stderr)


def log_tail(path: Path, lines: int = 12) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log available)"


def rotate(path: Path) -> None:
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def main() -> int:
    try:
        config = load_config()
    except Exception as exc:
        error_box(f"Configuration error:\n{exc}")
        return 1
    url = config.base_url + "/"

    state = probe(config.base_url)
    if state == "ours":
        webbrowser.open(url)
        return 0
    if state == "other":
        error_box(f"Port {config.port} is already used by another program.\n"
                  "Close that program, or set the AIMS_PORT environment variable to a free port.")
        return 1

    try:
        config.ensure_dirs()
    except OSError as exc:
        error_box(f"Cannot create the data folder {config.data_dir}:\n{exc}")
        return 1
    log_path = config.log_dir / "server.log"
    rotate(log_path)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "aims.server"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if probe(config.base_url) == "ours":
            webbrowser.open(url)
            return 0
        code = proc.poll()
        if code is not None:
            # Lost a race with another launcher: that server is (or soon will be) up.
            if code == EXIT_ALREADY_RUNNING:
                time.sleep(1)
                continue
            break
        time.sleep(0.3)

    error_box(f"The Apogee server did not start.\n\nLog: {log_path}\n\n{log_tail(log_path)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
