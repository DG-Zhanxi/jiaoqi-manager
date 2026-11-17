from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database
from .errors import DomainError
from .routers import auth, datasets, events, orders, plans, resources, schedules, workspaces
from .security import SlidingWindowLimiter

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _origin_allowed(origin: str, allowed: tuple[str, ...]) -> bool:
    normalized = origin.rstrip("/")
    return any(normalized == item.rstrip("/") for item in allowed)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = database or Database(settings.database_url, echo=settings.debug)
    database.create_all()

    app = FastAPI(title=settings.app_name, version="0.1.0")
    app.state.settings = settings
    app.state.db = database
    app.state.login_limiter = SlidingWindowLimiter()
    app.state.registration_limiter = SlidingWindowLimiter()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in UNSAFE_METHODS and origin and not _origin_allowed(
            origin, settings.allowed_origins
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "origin_not_allowed",
                        "message": "请求来源不被允许（严格 Origin 校验）",
                    }
                },
            )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": settings.app_name}

    for router in (
        auth.router,
        workspaces.router,
        datasets.router,
        orders.router,
        resources.router,
        schedules.router,
        plans.router,
        events.router,
    ):
        app.include_router(router)

    return app