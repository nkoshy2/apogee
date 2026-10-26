"""Protections for a server that any web page in the user's browser could try to reach.

- Host header allowlist: defeats DNS rebinding.
- State-changing requests need the X-AIMS-Request header. A cross-origin page cannot
  send a custom header without a CORS preflight, and this server never approves one.
- If an Origin header is present on a state-changing request, it must be our own.
- Strict CSP and related headers on every response.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

REQUEST_HEADER = "x-aims-request"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def install(app: FastAPI, port: int) -> None:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return PlainTextResponse("Invalid Host header", status_code=400)
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
            if request.headers.get(REQUEST_HEADER) != "1":
                return JSONResponse({"detail": "Missing X-AIMS-Request header"}, status_code=403)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
