"""Application factory for AI Lab Cloud.

Start with uvicorn:
    uvicorn ailab_cloud.main:app

All configuration comes from environment variables (see config.py).
The snap wrapper sets them from snap settings before exec'ing uvicorn.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
        description=(
            "Secure tunnel hub for remote access to AI Lab home devices."
        ),
        lifespan=lifespan,
    )

    # Store settings on app state so routes can access them via request.app.state
    app.state.settings = settings

    # Session middleware must be added before any route that uses request.session.
    # The secret comes from config — never hardcoded.
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)

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

    # Routers
    app.include_router(auth_router)
    app.include_router(tunnel_router)
    app.include_router(proxy_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/devices")
    async def list_devices(request):
        """List devices registered to the authenticated user."""
        from .auth import require_user
        user = require_user(request)
        registry = request.app.state.registry
        return await registry.list_user_devices(user)

    return app


app = create_app()
