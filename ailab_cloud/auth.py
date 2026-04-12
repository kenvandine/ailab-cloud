"""GitHub OAuth 2.0 authentication.

Flow:
  1. GET /auth/login        → redirects to GitHub
  2. GET /auth/callback     → exchanges code for token, stores github_login in session
  3. GET /auth/logout       → clears session
  4. GET /auth/me           → returns current user (or 401)
  5. GET /auth/tunnel-token → returns (or creates) the tunnel token for the authed user

The tunnel token is a random secret stored in Redis under token:{github_login}.
Home devices must present this token when registering their tunnel.
"""

import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

logger = logging.getLogger("ailab_cloud.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _settings(request: Request):
    return request.app.state.settings


def _registry(request: Request):
    return request.app.state.registry


def current_user(request: Request) -> str | None:
    """Return the logged-in GitHub login, or None."""
    return request.session.get("github_user")


def require_user(request: Request) -> str:
    """Dependency: return the logged-in user or raise 401."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/login")
async def login(request: Request):
    settings = _settings(request)
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    params = {
        "client_id": settings.github_client_id,
        "scope": "read:user",
        "state": state,
    }
    url = httpx.URL(_GITHUB_AUTHORIZE_URL).copy_merge_params(params)
    return RedirectResponse(str(url))


@router.get("/callback")
async def callback(request: Request, code: str, state: str):
    settings = _settings(request)

    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or not secrets.compare_digest(expected_state, state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    async with httpx.AsyncClient() as client:
        # Exchange code for access token
        token_resp = await client.post(
            _GITHUB_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="GitHub did not return a token")

        # Fetch user info
        user_resp = await client.get(
            _GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        user_resp.raise_for_status()
        github_user = user_resp.json().get("login")
        if not github_user:
            raise HTTPException(status_code=502, detail="Could not retrieve GitHub username")

    request.session["github_user"] = github_user
    logger.info("User %s logged in", github_user)
    return RedirectResponse("/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@router.get("/me")
async def me(user: str = Depends(require_user)):
    return {"github_user": user}


@router.get("/tunnel-token")
async def tunnel_token(request: Request, user: str = Depends(require_user)):
    """Return the tunnel registration token for this user.

    Creates one if it doesn't exist yet. The user copies this token
    to their home ailab instance:

        snap set ailab cloud.token=<token>
    """
    registry = _registry(request)
    token = await registry.get_or_create_token(user)
    return {"github_user": user, "token": token}


@router.post("/tunnel-token/regenerate")
async def regenerate_tunnel_token(request: Request, user: str = Depends(require_user)):
    """Invalidate and regenerate the tunnel token (e.g. if it was leaked)."""
    registry = _registry(request)
    token = await registry.regenerate_token(user)
    return {"github_user": user, "token": token}
