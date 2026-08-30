"""
Real-time event broadcasting for queue updates.

Provides:
  - WebSocketManager: manages WebSocket connections, broadcasts events
  - EventBus: lightweight pub/sub for decoupled event emission
  - SSE helpers: Server-Sent Events formatting

Architecture:
  Queue operations → EventBus.emit() → WebSocketManager.broadcast() → connected clients
                                        → SSE generators → polling clients

No external dependencies — uses only FastAPI/Starlette WebSocket + asyncio.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("Realtime")


# ── Event Bus ──────────────────────────────────────────────────
class EventBus:
    """
    Simple in-process pub/sub event bus.

    Components emit events like:
        EventBus.emit("queue:added", {"conversation_id": "c1", "priority_score": 0.8})

    Subscribers receive events via registered callbacks.
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._history: List[Dict[str, Any]] = []
        self._max_history = 200

    def on(self, event_type: str, callback: Callable) -> None:
        """Subscribe to an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)

    def off(self, event_type: str, callback: Callable) -> None:
        """Unsubscribe from an event type."""
        if event_type in self._subscribers:
            self._subscribers[event_type] = [
                cb for cb in self._subscribers[event_type] if cb != callback
            ]

    def emit(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Emit an event to all subscribers of that type, plus wildcard listeners."""
        event = {
            "type": event_type,
            "data": data or {},
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Store in history
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # Notify type-specific subscribers
        for cb in self._subscribers.get(event_type, []):
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Event callback error for {event_type}: {e}")

        # Notify wildcard subscribers
        for cb in self._subscribers.get("*", []):
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Wildcard callback error: {e}")

    def get_recent(self, count: int = 50) -> List[Dict[str, Any]]:
        """Get recent events from history."""
        return self._history[-count:]


# Global event bus singleton
event_bus = EventBus()


# ── WebSocket Manager ──────────────────────────────────────────
class WebSocketManager:
    """
    Manages WebSocket connections and broadcasts events.

    Clients connect to /ws/queue and receive JSON events:
    {
        "type": "queue:added" | "queue:popped" | "queue:removed" | "queue:updated" | "queue:breach",
        "data": { ... },
        "timestamp": "2024-01-15T10:30:00Z"
    }
    """

    def __init__(self):
        self._connections: Set[Any] = set()
        self._lock = asyncio.Lock()
        self._broadcast_count = 0

    async def connect(self, websocket) -> None:
        """Accept a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info(f"WebSocket connected (total: {len(self._connections)})")

        # Send welcome message
        await self._send_to(websocket, {
            "type": "connected",
            "data": {
                "message": "Connected to Sales Follow-Up Agent",
                "connections": len(self._connections),
            },
            "timestamp": datetime.utcnow().isoformat(),
        })

    async def disconnect(self, websocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            self._connections.discard(websocket)
        logger.info(f"WebSocket disconnected (total: {len(self._connections)})")

    async def broadcast(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Broadcast an event to all connected WebSocket clients."""
        if not self._connections:
            return

        message = {
            "type": event_type,
            "data": data or {},
            "timestamp": datetime.utcnow().isoformat(),
        }

        dead = []
        async with self._lock:
            for ws in list(self._connections):
                try:
                    await ws.send_json(message)
                    self._broadcast_count += 1
                except Exception:
                    dead.append(ws)

            # Clean up dead connections
            for ws in dead:
                self._connections.discard(ws)

        if dead:
            logger.debug(f"Cleaned up {len(dead)} dead WebSocket connections")

    async def _send_to(self, websocket, message: dict) -> None:
        """Send a message to a single WebSocket."""
        try:
            await websocket.send_json(message)
        except Exception:
            pass

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    @property
    def broadcast_count(self) -> int:
        return self._broadcast_count


# Global WebSocket manager singleton
ws_manager = WebSocketManager()


# ── SSE Helpers ────────────────────────────────────────────────
def format_sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Format an event as an SSE message."""
    payload = json.dumps({
        "type": event_type,
        "data": data,
        "timestamp": datetime.utcnow().isoformat(),
    })
    return f"event: {event_type}\ndata: {payload}\n\n"


# ── Queue Event Hooks ──────────────────────────────────────────
def emit_queue_event(event_type: str, **kwargs) -> None:
    """
    Emit a queue event to both the event bus and WebSocket manager.

    Call this from queue operations:
        emit_queue_event("queue:added", conversation_id="c1", priority_score=0.8)
    """
    data = {k: v for k, v in kwargs.items() if v is not None}

    # Emit to event bus (for SSE and subscribers)
    event_bus.emit(event_type, data)

    # Broadcast to WebSocket clients (fire-and-forget async) — safe with running loop detection
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # no running loop — skip WS broadcast (will be delivered via SSE / next poll)
            return
        loop.create_task(ws_manager.broadcast(event_type, data))
    except RuntimeError:
        pass
