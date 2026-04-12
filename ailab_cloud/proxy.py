"""HTTP and WebSocket proxy — routes browser traffic through the tunnel.

Routing modes
-------------
Path-based (always available, no DNS changes needed):
    /d/{device_id}/{path}           → AI Lab Web UI on the device (port 11500)
    /d/{device_id}:{port}/{path}    → specific port on the device

Host-header-based (requires wildcard DNS + reverse proxy in front):
    {device_id}.{domain}/{path}           → port 11500
    {device_id}-{port}.{domain}/{path}    → specific port

When running behind nginx with a wildcard certificate the middleware
parses the subdomain and injects `device_id` / `device_port` into
`request.state` so the catch-all routes can pick them up without
duplication.
"""

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import require_user

logger = logging.getLogger("ailab_cloud.proxy")

DEFAULT_PORT = 11500

# Hop-by-hop headers that must not be forwarded
_HOP_BY_HOP = frozenset({
    "host", "connection", "keep-alive", "transfer-encoding",
    "te", "trailers", "upgrade", "proxy-connection", "proxy-authenticate",
    "proxy-authorization",
})

router = APIRouter(tags=["proxy"])


# ── Host-header middleware ─────────────────────────────────────────────────────


class HostRoutingMiddleware(BaseHTTPMiddleware):
    """Extract device_id and target port from the Host header subdomain.

    Sets request.state.host_device_id and request.state.host_device_port
    when the host matches *.<domain>.  Path-based routes take priority;
    this middleware is purely additive.
    """

    def __init__(self, app, domain: str) -> None:
        super().__init__(app)
        self._domain = domain
        self._suffix = f".{domain}"

    async def dispatch(self, request: Request, call_next):
        request.state.host_device_id = None
        request.state.host_device_port = DEFAULT_PORT

        host = request.headers.get("host", "").split(":")[0].lower()
        if host.endswith(self._suffix):
            subdomain = host[: -len(self._suffix)]
            device_id, port = _parse_subdomain(subdomain)
            request.state.host_device_id = device_id
            request.state.host_device_port = port

        return await call_next(request)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_target(target: str) -> tuple[str, int]:
    """Parse "device_id" or "device_id:port" → (device_id, port)."""
    if ":" in target:
        device_id, _, port_str = target.rpartition(":")
        try:
            return device_id, int(port_str)
        except ValueError:
            pass
    return target, DEFAULT_PORT


def _parse_subdomain(subdomain: str) -> tuple[str, int]:
    """Parse "device_id" or "device_id-port" → (device_id, port).

    Uses a trailing -<digits> suffix for the port, e.g.
    "mybox-18789" → ("mybox", 18789).
    """
    if "-" in subdomain:
        head, _, tail = subdomain.rpartition("-")
        try:
            return head, int(tail)
        except ValueError:
            pass
    return subdomain, DEFAULT_PORT


def _strip_hop_by_hop(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


async def _do_proxy_http(request: Request, device_id: str, port: int, path: str, user: str):
    registry = request.app.state.registry

    owner = await registry.get_device_owner(device_id)
    if owner != user:
        raise HTTPException(status_code=403, detail="Access denied")

    if not registry.is_connected(device_id):
        raise HTTPException(status_code=502, detail="Device is not connected")

    body = await request.body()
    headers = _strip_hop_by_hop(dict(request.headers))

    full_path = f"/{path}" if path else "/"
    if request.url.query:
        full_path += f"?{request.url.query}"

    try:
        resp = await registry.proxy_request(
            device_id=device_id,
            method=request.method,
            path=full_path,
            port=port,
            headers=headers,
            body=body,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    resp_body = base64.b64decode(resp.get("body", ""))
    resp_headers = _strip_hop_by_hop(resp.get("headers", {}))

    return Response(
        content=resp_body,
        status_code=resp.get("status", 200),
        headers=resp_headers,
        media_type=None,  # honour whatever the device returned
    )


async def _do_proxy_ws(websocket: WebSocket, device_id: str, port: int, path: str, user: str):
    registry = websocket.app.state.registry

    owner = await registry.get_device_owner(device_id)
    if owner != user:
        await websocket.close(code=1008, reason="Access denied")
        return

    if not registry.is_connected(device_id):
        await websocket.close(code=1011, reason="Device is not connected")
        return

    await websocket.accept()
    full_path = f"/{path}" if path else "/"
    await registry.proxy_websocket(
        device_id=device_id,
        path=full_path,
        port=port,
        client_ws=websocket,
    )


# ── Path-based routes ─────────────────────────────────────────────────────────


@router.api_route(
    "/d/{target}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_http_path(
    target: str,
    path: str,
    request: Request,
    user: str = Depends(require_user),
):
    """Proxy an HTTP request to a home device using path-based routing."""
    device_id, port = _parse_target(target)
    return await _do_proxy_http(request, device_id, port, path, user)


@router.websocket("/d/{target}/ws/{path:path}")
async def proxy_ws_path(target: str, path: str, websocket: WebSocket):
    """Proxy a WebSocket connection to a home device using path-based routing."""
    user = websocket.session.get("github_user")
    if not user:
        await websocket.close(code=1008, reason="Not authenticated")
        return
    device_id, port = _parse_target(target)
    await _do_proxy_ws(websocket, device_id, port, path, user)


# ── Host-header routes (wildcard subdomain) ───────────────────────────────────


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_http_host(
    path: str,
    request: Request,
    user: str = Depends(require_user),
):
    """Proxy an HTTP request when routed via a wildcard subdomain Host header."""
    device_id = request.state.host_device_id
    if not device_id:
        raise HTTPException(status_code=404, detail="Not found")
    port = request.state.host_device_port
    return await _do_proxy_http(request, device_id, port, path, user)


@router.websocket("/ws/{path:path}")
async def proxy_ws_host(path: str, websocket: WebSocket):
    """Proxy a WebSocket connection when routed via a wildcard subdomain."""
    device_id = websocket.state.host_device_id
    if not device_id:
        await websocket.close(code=1008, reason="No device resolved from Host header")
        return
    user = websocket.session.get("github_user")
    if not user:
        await websocket.close(code=1008, reason="Not authenticated")
        return
    port = websocket.state.host_device_port
    await _do_proxy_ws(websocket, device_id, port, path, user)
