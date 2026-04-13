"""Application factory for AI Lab Cloud.

Start with uvicorn:
    uvicorn ailab_cloud.main:app

All configuration comes from environment variables (see config.py).
The snap wrapper sets them from snap settings before exec'ing uvicorn.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import router as auth_router
from .config import load as load_settings
from .proxy import HostRoutingMiddleware, router as proxy_router
from .tunnel import TunnelRegistry, router as tunnel_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ailab_cloud")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    registry = TunnelRegistry(settings.redis_url)
    app.state.registry = registry
    await registry.connect()
    logger.info("AI Lab Cloud started — domain: %s", settings.domain)
    yield
    await registry.close()
    logger.info("AI Lab Cloud stopped")


def create_app() -> FastAPI:
    settings = load_settings()

    app = FastAPI(
        title="AI Lab Cloud",
        description="Secure tunnel hub for remote access to AI Lab home devices.",
        lifespan=lifespan,
    )

    app.state.settings = settings

    # Session middleware must be added before any route that uses request.session.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.session_https_only,
        same_site="lax",
    )

    # CORS: allow the hub's own domain and all device subdomains.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=rf"https://([a-z0-9-]+\.)?{settings.domain.replace('.', r'\.')}",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Host-header routing middleware (for wildcard subdomain deployments).
    app.add_middleware(HostRoutingMiddleware, domain=settings.domain)

    # ── Static routes (defined before proxy catch-all) ────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index(request: Request, error: str = ""):
        """Home page — login screen or dashboard depending on auth state."""
        user = request.session.get("github_user")
        domain = request.app.state.settings.domain

        if not user:
            return templates.TemplateResponse(request, "login.html", {
                "domain": domain,
                "error": error,
            })

        registry = request.app.state.registry
        devices = await registry.list_user_devices(user)
        token = await registry.get_or_create_token(user)

        return templates.TemplateResponse(request, "dashboard.html", {
            "user": user,
            "devices": devices,
            "token": token,
            "domain": domain,
        })

    @app.get("/api/devices")
    async def list_devices(request: Request):
        """List devices registered to the authenticated user (JSON)."""
        from .auth import require_user
        user = require_user(request)
        registry = request.app.state.registry
        return await registry.list_user_devices(user)

    # ── Routers (proxy catch-all last) ────────────────────────────────────────
    app.include_router(auth_router)
    app.include_router(tunnel_router)
    app.include_router(proxy_router)

    return app


app = create_app()
