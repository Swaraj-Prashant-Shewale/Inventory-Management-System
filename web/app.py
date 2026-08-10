"""FastAPI application factory."""
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    from web import routes_admin, routes_auth, routes_main

    app = FastAPI(
        title="Inventory Management System",
        docs_url=None, redoc_url=None, openapi_url=None,   # no public API surface yet
    )

    app.include_router(routes_auth.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_main.router)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        # Never leak internals to the browser; the log has the traceback.
        return HTMLResponse(
            "<h1>Something went wrong</h1>"
            "<p>The error has been logged. Go back and try again.</p>",
            status_code=500,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'")
        return response

    return app


app = create_app()
