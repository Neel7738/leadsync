"""Priority queue with SLA tracking for follow-up prospects."""
import logging
from datetime import datetime
from typing import List, Optional
from ..models.prospect import ScoredProspect

logger = logging.getLogger("Queue")


def _emit(event_type: str, **kwargs) -> None:
    """Emit a queue event to realtime bus and audit trail."""
    # Real-time broadcast
    try:
        from ..realtime import emit_queue_event
        emit_queue_event(event_type, **kwargs)
    except Exception:
        pass
    # Audit trail
    try:
        from ..database.audit import audit
        cid = kwargs.get("conversation_id")
        audit.log(
            action=event_type,
            entity_type="prospect",
            entity_id=cid,
            details={k: v for k, v in kwargs.items() if k != "conversation_id"},
        )
    except Exception:
        pass


class PriorityQueue:
    """In-memory priority queue for scored prospects with SLA tracking."""

    def __init__(self, max_items: int | None = None):
        self._items: dict[str, ScoredProspect] = {}
        self._max_items_override = max_items

    def add(self, scored: ScoredProspect) -> None:
        """Add or update a scored prospect. Enforces QUEUE_MAX_ITEMS by evicting lowest priority."""
        if not isinstance(scored, ScoredProspect):
            raise TypeError("expected ScoredProspect")
        cid = str(scored.conversation_id)
        # Enforce max size if configured
        if getattr(self, "_max_items_override", None) is not None:
            max_items = self._max_items_override
        else:
            try:
                from ..config import get_settings as _gs
                max_items = getattr(_gs(), "queue_max_items_per_rep", 50)
            except Exception:
                max_items = 50
        if cid not in self._items and len(self._items) >= max_items:
            # evict lowest priority non-breached first
            evict_candidate = sorted(self._items.values(), key=lambda s: (s.sla_breached, s.priority_score or 0))[0]
            evict_id = str(evict_candidate.conversation_id)
            del self._items[evict_id]
            _emit("queue:evicted", conversation_id=evict_id, reason="queue_full", total_size=self.size())
            logger.warning(f"Queue at capacity ({max_items}) — evicted lowest priority {evict_id}")
        self._items[cid] = scored
        _emit(
            "queue:added",
            conversation_id=cid,
            priority_score=scored.priority_score,
            urgency=getattr(scored.conversation, "urgency", "unknown") if scored.conversation else "unknown",
            total_size=self.size(),
        )

    def pop_next(self) -> Optional[ScoredProspect]:
        """Pop highest-priority non-breached prospect. SLA-breached first."""
        self._refresh_breaches()
        if not self._items:
            return None
        items = sorted(
            self._items.values(),
            key=lambda s: (not s.sla_breached, -(s.priority_score or 0)),
        )
        nxt = items[0]
        cid = str(nxt.conversation_id)
        del self._items[cid]
        _emit(
            "queue:popped",
            conversation_id=cid,
            priority_score=nxt.priority_score,
            total_size=self.size(),
        )
        return nxt

    def get_breached(self) -> List[ScoredProspect]:
        """Return all SLA-breached prospects."""
        self._refresh_breaches()
        return [s for s in self._items.values() if s.sla_breached]

    def list(self) -> List[ScoredProspect]:
        """List all items sorted by priority."""
        self._refresh_breaches()
        return sorted(
            self._items.values(),
            key=lambda s: (not s.sla_breached, -(s.priority_score or 0)),
        )

    def size(self) -> int:
        return len(self._items)

    def get_by_id(self, conversation_id: str) -> Optional[ScoredProspect]:
        """Get a prospect by conversation ID."""
        return self._items.get(str(conversation_id))

    def remove(self, conversation_id: str) -> bool:
        """Remove a prospect by conversation ID."""
        key = str(conversation_id)
        if key in self._items:
            del self._items[key]
            _emit(
                "queue:removed",
                conversation_id=key,
                total_size=self.size(),
            )
            return True
        return False

    def increment_requeue(self, conversation_id: str) -> Optional[dict]:
        """Increment requeue counter for a prospect."""
        item = self.get_by_id(conversation_id)
        if item is None:
            return None
        item.times_requeued = (item.times_requeued or 0) + 1
        return {
            "conversation_id": conversation_id,
            "times_requeued": item.times_requeued,
        }

    def check_sla_breaches(self) -> List[dict]:
        """Check for SLA breaches and return list of breached items."""
        breached = self.get_breached()
        return [
            {
                "conversation_id": s.conversation_id,
                "priority_score": s.priority_score,
                "sla_deadline": s.sla_deadline.isoformat() if s.sla_deadline else None,
                "times_requeued": s.times_requeued,
            }
            for s in breached
        ]

    def get_queue_stats(self) -> dict:
        """Return queue statistics."""
        self._refresh_breaches()
        breached = [s for s in self._items.values() if s.sla_breached]
        return {
            "total_items": len(self._items),
            "breached_count": len(breached),
            "avg_priority": (
                round(sum(s.priority_score for s in self._items.values()) / len(self._items), 4)
                if self._items
                else 0.0
            ),
        }

    def _refresh_breaches(self) -> None:
        now = datetime.utcnow()
        for s in self._items.values():
            if s.sla_deadline and not s.sla_breached and now > s.sla_deadline:
                s.sla_breached = True
                s.times_requeued = (s.times_requeued or 0) + 1
                s.status = "breached"
                _emit(
                    "queue:breach",
                    conversation_id=str(s.conversation_id),
                    priority_score=s.priority_score,
                    times_requeued=s.times_requeued,
                )


_queue = None


def get_queue():
    """
    Get the global priority queue singleton.

    Auto-selects backend based on settings:
    - If USE_REDIS=true and redis is available → RedisPriorityQueue
    - Otherwise → in-memory PriorityQueue

    Returns either PriorityQueue or RedisPriorityQueue (same interface).
    """
    global _queue
    if _queue is not None:
        return _queue

    try:
        from ..config import get_settings
        settings = get_settings()
    except Exception:
        settings = None

    # Try Redis if configured
    if settings and getattr(settings, "use_redis", False):
        try:
            from .redis_queue import RedisPriorityQueue
            _queue = RedisPriorityQueue(
                redis_url=getattr(settings, "redis_url", "redis://localhost:6379"),
            )
            return _queue
        except ImportError:
            logger.warning("redis package not installed — falling back to in-memory queue")
        except Exception as e:
            logger.warning(f"Redis connection failed ({e}) — falling back to in-memory queue")

    _queue = PriorityQueue()
    return _queue


__all__ = ["PriorityQueue", "get_queue"]
