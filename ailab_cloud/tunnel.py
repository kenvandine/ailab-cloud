"""Tunnel registry and WebSocket control-plane handler.

Architecture
------------
Home devices connect via WebSocket to /tunnel/register and keep the
connection open indefinitely. The hub uses these persistent connections
to route browser traffic back to the home device.

Protocol — messages are JSON over WebSocket text frames.

Home device → Hub:
  {"type": "register",  "github_user": "...", "device_id": "...",
                        "ports": [...], "token": "..."}
  {"type": "response",  "id": "<uuid>", "status": 200,
                        "headers": {...}, "body": "<base64>"}
  {"type": "ws_opened", "conn_id": "<uuid>"}
  {"type": "ws_error",  "conn_id": "<uuid>", "error": "..."}
  {"type": "ws_frame",  "conn_id": "<uuid>", "opcode": 1|2, "data": "<base64>"}
  {"type": "ws_close",  "conn_id": "<uuid>"}

Hub → Home device:
  {"type": "registered"}
  {"type": "request",   "id": "<uuid>", "method": "...", "path": "...",
                        "port": 11500, "headers": {...}, "body": "<base64>"}
  {"type": "ws_open",   "conn_id": "<uuid>", "port": ..., "path": "..."}
  {"type": "ws_frame",  "conn_id": "<uuid>", "opcode": 1|2, "data": "<base64>"}
  {"type": "ws_close",  "conn_id": "<uuid>"}
"""

import asyncio
import base64
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("ailab_cloud.tunnel")

router = APIRouter(tags=["tunnel"])

_DEVICE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_GITHUB_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


# ── Device metadata ───────────────────────────────────────────────────────────


@dataclass
class DeviceInfo:
    device_id: str
    github_user: str
    ports: list[int]


@dataclass
class PendingRequest:
    device_id: str
    tunnel_ws: WebSocket
    future: asyncio.Future


@dataclass
class WebSocketRelay:
    device_id: str
    tunnel_ws: WebSocket
    queue: asyncio.Queue


def _normalize_ports(raw_ports: object) -> list[int]:
    if not isinstance(raw_ports, list):
        raise ValueError("ports must be a list of integers")

    ports: list[int] = []
    seen: set[int] = set()
    for raw in raw_ports:
        if not isinstance(raw, int):
            raise ValueError("ports must contain integers")
        if not 1 <= raw <= 65535:
            raise ValueError("ports must be between 1 and 65535")
        if raw not in seen:
            ports.append(raw)
            seen.add(raw)

    if not ports:
        raise ValueError("at least one port is required")

    return ports


def _parse_bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _extract_tunnel_token(ws: WebSocket, msg: dict) -> str:
    auth_header = _parse_bearer_token(ws.headers.get("authorization", ""))
    if auth_header:
        return auth_header

    token = msg.get("token", "")
    if isinstance(token, str) and token.strip():
        return token.strip()

    legacy_token = ws.query_params.get("token", "").strip()
    if legacy_token:
        logger.warning("Tunnel client for %s is still sending its token in the URL", ws.client)
    return legacy_token


# ── Registry ──────────────────────────────────────────────────────────────────


class TunnelRegistry:
    """Central registry of active home-device tunnels.

    Active WebSocket connections are stored in-memory; device metadata
    (owner, port list) is also written to Redis so it survives a hub
    restart and can be inspected externally.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None

        # device_id → active WebSocket
        self._connections: dict[str, WebSocket] = {}

        # device_id → github_user (fast in-memory lookup)
        self._device_owners: dict[str, str] = {}

        # device_id → advertised ports
        self._device_ports: dict[str, tuple[int, ...]] = {}

        # request_id → asyncio.Future waiting for an HTTP response frame
        self._pending: dict[str, PendingRequest] = {}

        # conn_id → asyncio.Queue relaying WS frames from home to hub
        self._ws_queues: dict[str, WebSocketRelay] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        await self._redis.ping()
        logger.info("Connected to Redis at %s", self._redis_url)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    # ── Token management ──────────────────────────────────────────────────────

    async def get_or_create_token(self, github_user: str) -> str:
        """Return the tunnel token for *github_user*, creating one if absent."""
        key = f"token:{github_user}"
        token = await self._redis.get(key)
        if not token:
            token = secrets.token_urlsafe(32)
            await self._redis.set(key, token)
        return token

    async def regenerate_token(self, github_user: str) -> str:
        """Invalidate the existing token and issue a fresh one."""
        token = secrets.token_urlsafe(32)
        await self._redis.set(f"token:{github_user}", token)
        return token

    async def _validate_token(self, github_user: str, token: str) -> bool:
        stored = await self._redis.get(f"token:{github_user}")
        if not stored:
            return False
        return secrets.compare_digest(stored, token)

    # ── Device queries ────────────────────────────────────────────────────────

    async def get_device_owner(self, device_id: str) -> str | None:
        """Return the GitHub username that owns *device_id*, or None."""
        # Check in-memory first (fast path for connected devices)
        owner = self._device_owners.get(device_id)
        if owner:
            return owner
        # Fall back to Redis (device registered but currently offline)
        return await self._redis.hget(f"device:{device_id}", "github_user")

    async def get_device_ports(self, device_id: str) -> list[int]:
        ports = self._device_ports.get(device_id)
        if ports is not None:
            return list(ports)

        raw_ports = await self._redis.hget(f"device:{device_id}", "ports")
        if not raw_ports:
            return []

        try:
            return _normalize_ports(json.loads(raw_ports))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Device %s has invalid stored port metadata", device_id)
            return []

    async def is_port_allowed(self, device_id: str, port: int) -> bool:
        return port in await self.get_device_ports(device_id)

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._connections

    async def list_user_devices(self, github_user: str) -> list[dict]:
        device_ids = await self._redis.smembers(f"user:{github_user}:devices")
        result = []
        for did in device_ids:
            info = await self._redis.hgetall(f"device:{did}")
            if info:
                result.append({
                    "device_id": did,
                    "github_user": info.get("github_user"),
                    "ports": json.loads(info.get("ports", "[]")),
                    "connected": self.is_connected(did),
                })
        return result

    # ── Tunnel WebSocket handler ───────────────────────────────────────────────

    async def handle_tunnel(self, ws: WebSocket) -> None:
        """Accept and manage a tunnel connection from a home device."""
        await ws.accept()

        # Wait for the registration message (give the client 10 s)
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except asyncio.TimeoutError:
            await ws.close(code=1008, reason="Registration timeout")
            return
        except Exception:
            await ws.close(code=1011, reason="Unexpected error during registration")
            return

        if msg.get("type") != "register":
            await ws.close(code=1008, reason="Expected register message")
            return

        github_user: str = msg.get("github_user", "").strip()
        device_id: str = msg.get("device_id", "").strip()
        token = _extract_tunnel_token(ws, msg)

        if not github_user or not device_id or not token:
            await ws.close(code=1008, reason="github_user, device_id, and token are required")
            return

        if not _GITHUB_USER_RE.fullmatch(github_user):
            await ws.close(code=1008, reason="Invalid github_user")
            return

        if not _DEVICE_ID_RE.fullmatch(device_id):
            await ws.close(code=1008, reason="Invalid device_id")
            return

        try:
            ports = _normalize_ports(msg.get("ports", [11500]))
        except ValueError as exc:
            await ws.close(code=1008, reason=str(exc))
            return

        if not await self._validate_token(github_user, token):
            await ws.close(code=1008, reason="Invalid token")
            return

        existing_owner = await self.get_device_owner(device_id)
        if existing_owner and existing_owner != github_user:
            await ws.close(code=1008, reason="Device ID is already claimed by another user")
            return

        # Persist and register
        prior_ws = self._connections.get(device_id)
        self._connections[device_id] = ws
        self._device_owners[device_id] = github_user
        self._device_ports[device_id] = tuple(ports)

        await self._redis.hset(f"device:{device_id}", mapping={
            "github_user": github_user,
            "ports": json.dumps(ports),
        })
        await self._redis.sadd(f"user:{github_user}:devices", device_id)

        if prior_ws is not None and prior_ws is not ws:
            try:
                await prior_ws.close(code=1012, reason="Tunnel replaced by a new connection")
            except Exception:
                logger.debug("Failed to close previous tunnel for %s", device_id)

        logger.info("Device %s registered for user %s (ports: %s)",
                    device_id, github_user, ports)
        await ws.send_json({"type": "registered"})

        # Drive the receive loop until the connection closes
        try:
            await self._receive_loop(device_id, ws)
        finally:
            if self._connections.get(device_id) is ws:
                self._connections.pop(device_id, None)
                self._device_owners.pop(device_id, None)
                self._device_ports.pop(device_id, None)
                logger.info("Device %s disconnected", device_id)

    async def _receive_loop(self, device_id: str, ws: WebSocket) -> None:
        try:
            async for raw in ws.iter_text():
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Device %s sent non-JSON frame", device_id)
                    continue

                msg_type = data.get("type")

                if msg_type == "response":
                    # An HTTP response returning from the home device
                    req_id: str = data.get("id", "")
                    pending = self._pending.pop(req_id, None)
                    if pending and not pending.future.done():
                        pending.future.set_result(data)

                elif msg_type in ("ws_frame", "ws_opened", "ws_error", "ws_close"):
                    # A WebSocket relay message
                    conn_id: str = data.get("conn_id", "")
                    relay = self._ws_queues.get(conn_id)
                    if relay:
                        await relay.queue.put(data)

                else:
                    logger.debug("Device %s: unhandled message type %r", device_id, msg_type)

        except WebSocketDisconnect:
            pass

        # Unblock any callers that are waiting on this device
        for req_id, pending in list(self._pending.items()):
            if pending.tunnel_ws is not ws:
                continue
            self._pending.pop(req_id, None)
            if not pending.future.done():
                pending.future.set_exception(RuntimeError(f"Device {device_id} disconnected"))

        for conn_id, relay in list(self._ws_queues.items()):
            if relay.tunnel_ws is not ws:
                continue
            self._ws_queues.pop(conn_id, None)
            await relay.queue.put({
                "type": "ws_close",
                "conn_id": conn_id,
                "reason": "device disconnected",
            })

    # ── HTTP proxying ─────────────────────────────────────────────────────────

    async def proxy_request(
        self,
        device_id: str,
        method: str,
        path: str,
        port: int,
        headers: dict,
        body: bytes,
    ) -> dict:
        """Forward an HTTP request through the tunnel; return the response dict."""
        ws = self._connections.get(device_id)
        if ws is None:
            raise RuntimeError(f"Device '{device_id}' is not connected")

        req_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[req_id] = PendingRequest(
            device_id=device_id,
            tunnel_ws=ws,
            future=future,
        )

        envelope = {
            "type": "request",
            "id": req_id,
            "method": method,
            "path": path,
            "port": port,
            "headers": headers,
            "body": base64.b64encode(body).decode(),
        }

        try:
            await ws.send_json(envelope)
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError("Tunnel request timed out")
        finally:
            pending = self._pending.get(req_id)
            if pending and pending.future is future:
                self._pending.pop(req_id, None)

    # ── WebSocket proxying ────────────────────────────────────────────────────

    async def proxy_websocket(
        self,
        device_id: str,
        path: str,
        port: int,
        client_ws: WebSocket,
        headers: dict | None = None,
    ) -> None:
        """Proxy a browser WebSocket connection through the tunnel."""
        tunnel_ws = self._connections.get(device_id)
        if tunnel_ws is None:
            await client_ws.close(code=1011, reason="Device not connected")
            return

        conn_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        relay = WebSocketRelay(device_id=device_id, tunnel_ws=tunnel_ws, queue=queue)
        self._ws_queues[conn_id] = relay

        # Ask the home device to open a WebSocket to the target path/port.
        # Forward selected browser headers (e.g. Origin) so local services
        # that enforce CORS on WS upgrades receive the real browser context.
        envelope: dict = {
            "type": "ws_open",
            "conn_id": conn_id,
            "port": port,
            "path": path,
        }
        if headers:
            envelope["headers"] = headers
        await tunnel_ws.send_json(envelope)

        # Wait for the home device to confirm the connection is open
        try:
            ack = await asyncio.wait_for(queue.get(), timeout=10)
        except asyncio.TimeoutError:
            current = self._ws_queues.get(conn_id)
            if current is relay:
                self._ws_queues.pop(conn_id, None)
            await client_ws.close(code=1011, reason="WS open timed out")
            return

        if ack.get("type") == "ws_error":
            current = self._ws_queues.get(conn_id)
            if current is relay:
                self._ws_queues.pop(conn_id, None)
            await client_ws.close(code=1011, reason=ack.get("error", "WS open failed"))
            return

        # Relay frames in both directions concurrently
        async def client_to_tunnel():
            try:
                while True:
                    message = await client_ws.receive()
                    mtype = message.get("type")
                    if mtype == "websocket.disconnect":
                        break
                    if mtype != "websocket.receive":
                        continue
                    text = message.get("text")
                    data = message.get("bytes")
                    if text is not None:
                        await tunnel_ws.send_json({
                            "type": "ws_frame",
                            "conn_id": conn_id,
                            "opcode": 1,  # text
                            "data": base64.b64encode(text.encode()).decode(),
                        })
                    elif data is not None:
                        await tunnel_ws.send_json({
                            "type": "ws_frame",
                            "conn_id": conn_id,
                            "opcode": 2,  # binary
                            "data": base64.b64encode(data).decode(),
                        })
            except WebSocketDisconnect:
                pass
            finally:
                # Tell the home device this browser WS is gone
                try:
                    await tunnel_ws.send_json({"type": "ws_close", "conn_id": conn_id})
                except Exception:
                    pass

        async def tunnel_to_client():
            try:
                while True:
                    msg = await queue.get()
                    mtype = msg.get("type")
                    if mtype == "ws_close":
                        await client_ws.close()
                        break
                    if mtype != "ws_frame":
                        continue
                    data = base64.b64decode(msg.get("data", ""))
                    opcode = msg.get("opcode", 1)
                    if opcode == 1:
                        await client_ws.send_text(data.decode())
                    else:
                        await client_ws.send_bytes(data)
            except WebSocketDisconnect:
                pass

        try:
            await asyncio.gather(client_to_tunnel(), tunnel_to_client())
        finally:
            current = self._ws_queues.get(conn_id)
            if current is relay:
                self._ws_queues.pop(conn_id, None)


# ── FastAPI route ─────────────────────────────────────────────────────────────


@router.websocket("/tunnel/register")
async def tunnel_register(websocket: WebSocket):
    """Endpoint for home devices to establish their persistent tunnel.

    The home device presents its tunnel token in the Authorization header
    or in the initial register message.
    """
    registry: TunnelRegistry = websocket.app.state.registry
    await registry.handle_tunnel(websocket)
