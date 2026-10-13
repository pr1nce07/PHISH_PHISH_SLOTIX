"""Application entry point: security middleware, safe error handling, routers and the static UI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import api_admin, api_public, services
from .config import BASE_DIR, settings
from .db import SessionLocal

log = logging.getLogger("timetable")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# The theme's typefaces (Spectral, IBM Plex) come from Google Fonts: a stylesheet and font files only, never scripts.
_FONT_CSS, _FONT_FILES = "https://fonts.googleapis.com", "https://fonts.gstatic.com"
if settings.clerk_enabled:
    # Clerk's hosted components need its Frontend API, its bot-protection frame and inline styles.
    _fapi = f"https://{settings.clerk_frontend_api}"
    CSP = (f"default-src 'self'; script-src 'self' {_fapi} https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline' {_FONT_CSS}; "
           f"img-src 'self' data: https://img.clerk.com; connect-src 'self' {_fapi}; worker-src 'self' blob:; "
           f"frame-src https://challenges.cloudflare.com; font-src 'self' {_FONT_FILES}; object-src 'none'; base-uri 'self'; "
           "form-action 'self'; frame-ancestors 'none'")
else:
    CSP = (f"default-src 'self'; script-src 'self'; style-src 'self' {_FONT_CSS}; img-src 'self' data:; connect-src 'self'; "
           f"font-src 'self' {_FONT_FILES}; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        services.recover_stale_runs()
    except Exception:  # noqa: BLE001 - schema may not be migrated yet; /readyz reports it
        log.warning("database not ready - run `alembic upgrade head`")
    yield


app = FastAPI(title="Academic Timetable Generator", version="1.0.0", lifespan=lifespan,
              docs_url="/docs" if settings.docs_enabled else None, redoc_url=None,
              openapi_url="/openapi.json" if settings.docs_enabled else None)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # size cap (cheap DoS guard) and same-origin check (defence in depth on top of SameSite + CSRF token)
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > settings.max_body_bytes:
            return JSONResponse({"detail": "Request body too large."}, status_code=413)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
    response = await call_next(request)
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if not request.url.path.startswith(DOC_PATHS):
        h["Content-Security-Policy"] = CSP
    if settings.cookie_secure:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api"):
        h["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(_request: Request, exc: RequestValidationError):
    # never echo the submitted input back
    return JSONResponse({"detail": [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]},
                        status_code=422)


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    log.exception("unhandled error", exc_info=exc)
    return JSONResponse({"detail": "Internal server error."}, status_code=500)  # no stack traces / SQL to the client


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1 FROM app_roles LIMIT 1"))
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "not ready")
    return {"status": "ready"}


app.include_router(api_public.router)
app.include_router(api_admin.router)


WEB = BASE_DIR / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
