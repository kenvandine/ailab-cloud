"""Runtime configuration loaded exclusively from environment variables.

All settings must be injected at startup — nothing is hardcoded.
The snap wrapper (snap/local/ailab-cloud-wrapper) reads snap settings
and exports them as environment variables before exec'ing uvicorn.
"""

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # Required — service refuses to start without these.
    domain: str             # base domain, e.g. "cloud.example.com"
    github_client_id: str
    github_client_secret: str
    session_secret: str     # used by Starlette SessionMiddleware to sign cookies

    # Optional with sane defaults.
    redis_url: str = "redis://localhost:6379"
    host: str = "0.0.0.0"
    port: int = 8080


def load() -> Settings:
    """Load and validate settings from environment.

    Raises RuntimeError listing every missing variable so the operator
    can fix them all in one go rather than one-at-a-time.
    """
    missing: list[str] = []

    def req(key: str) -> str:
        val = os.environ.get(key, "").strip()
        if not val:
            missing.append(key)
        return val

    settings = Settings(
        domain=req("AILAB_CLOUD_DOMAIN"),
        github_client_id=req("AILAB_CLOUD_GITHUB_CLIENT_ID"),
        github_client_secret=req("AILAB_CLOUD_GITHUB_CLIENT_SECRET"),
        session_secret=req("AILAB_CLOUD_SESSION_SECRET"),
        redis_url=os.environ.get("AILAB_CLOUD_REDIS_URL", "redis://localhost:6379"),
        host=os.environ.get("AILAB_CLOUD_HOST", "0.0.0.0"),
        port=int(os.environ.get("AILAB_CLOUD_PORT", "8080")),
    )

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return settings
