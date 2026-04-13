"""Tunnel registry and WebSocket control-plane handler.

Architecture
------------
Home devices connect via WebSocket to /tunnel/register and keep the
connection open indefinitely. The hub uses these persistent connections
to route browser traffic back to the home device.

Protocol — messages are JSON over WebSocket text frames.

Home device → Hub:
  {"type": "register",  "github_user": "...", "device_id": "...", "ports": [...]}
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
import secrets
import uuid
from dataclasses import dataclass, field

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("ailab_cloud.tunnel")

router = APIRouter(tags=["tunnel"])


# ── Device metadata ───────────────────────────────────────────────────────────


@dataclass
class DeviceInfo:
    device_id: str
    github_user: str
    ports: list[int]


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

        # request_id → asyncio.Future waiting for an HTTP response frame
        self._pending: dict[str, asyncio.Future] = {}

        # conn_id → asyncio.Queue relaying WS frames from home to hub
        self._ws_queues: dict[str, asyncio.Queue] = {}

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

    async def handle_tunnel(self, ws: WebSocket, token: str) -> None:
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
        ports: list[int] = msg.get("ports", [11500])

        if not github_user or not device_id:
            await ws.close(code=1008, reason="github_user and device_id are required")
            return

        if not await self._validate_token(github_user, token):
            await ws.close(code=1008, reason="Invalid token")
            return

        # Persist and register
        self._connections[device_id] = ws
        self._device_owners[device_id] = github_user

        await self._redis.hset(f"device:{device_id}", mapping={
            "github_user": github_user,
            "ports": json.dumps(ports),
        })
        await self._redis.sadd(f"user:{github_user}:devices", device_id)

        logger.info("Device %s registered for user %s (ports: %s)",
                    device_id, github_user, ports)
        await ws.send_json({"type": "registered"})

        # Drive the receive loop until the connection closes
        try:
            await self._receive_loop(device_id, ws)
        finally:
            self._connections.pop(device_id, None)
            self._device_owners.pop(device_id, None)
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
                    future = self._pending.pop(req_id, None)
                    if future and not future.done():
                        future.set_result(data)

                elif msg_type in ("ws_frame", "ws_opened", "ws_error", "ws_close"):
                    # A WebSocket relay message
                    conn_id: str = data.get("conn_id", "")
                    queue = self._ws_queues.get(conn_id)
                    if queue:
                        await queue.put(data)

                else:
                    logger.debug("Device %s: unhandled message type %r", device_id, msg_type)

        except WebSocketDisconnect:
            pass

        # Unblock any callers that are waiting on this device
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(f"Device {device_id} disconnected"))
        self._pending.clear()

        for queue in self._ws_queues.values():
            await queue.put({"type": "ws_close", "conn_id": None, "reason": "device disconnected"})

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
        self._pending[req_id] = future

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
        self._ws_queues[conn_id] = queue

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
            self._ws_queues.pop(conn_id, None)
            await client_ws.close(code=1011, reason="WS open timed out")
            return

        if ack.get("type") == "ws_error":
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
            self._ws_queues.pop(conn_id, None)


# ── FastAPI route ─────────────────────────────────────────────────────────────


@router.websocket("/tunnel/register")
async def tunnel_register(websocket: WebSocket, token: str):
    """Endpoint for home devices to establish their persistent tunnel.

    The home device passes its tunnel token as a query parameter:
        wss://<hub>/tunnel/register?token=<token>
    """
    registry: TunnelRegistry = websocket.app.state.registry
    await registry.handle_tunnel(websocket, token)
