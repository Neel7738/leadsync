"""
Redis-backed priority queue for the sales follow-up agent.

Uses Redis sorted sets for O(log N) priority ordering and hash maps for
prospect data storage. Provides the same interface as the in-memory
PriorityQueue so the rest of the codebase is backend-agnostic.

Redis key schema:
    queue:prospects         — sorted set (score = -priority_score, member = conversation_id)
    queue:data:{id}         — hash with serialized prospect JSON
    queue:meta              — hash with queue metadata (stats cache)

Design choices:
    - Sorted set score = -priority_score so ZRANGE gives highest priority first
    - SLA breach detection runs on read operations (lazy evaluation)
    - Each prospect stored as JSON blob in a hash for full round-trip fidelity
    - Pipeline transactions for atomic multi-key operations
"""

import json
import logging
from datetime import datetime
from typing import List, Optional, Any

logger = logging.getLogger("RedisQueue")

# Redis key prefixes
KEY_QUEUE = "queue:prospects"
KEY_DATA_PREFIX = "queue:data:"
KEY_META = "queue:meta"


class RedisPriorityQueue:
    """
    Redis-backed priority queue with SLA tracking.

    Same interface as PriorityQueue but backed by Redis sorted sets.
    Requires `redis` package: pip install redis
    """

    def __init__(self, redis_url: str = "redis://localhost:6379", prefix: str = "sfu"):
        """
        Args:
            redis_url: Redis connection URL
            prefix: Key prefix to namespace all queue keys (allows multiple queues)
        """
        try:
            import redis
        except ImportError:
            raise ImportError(
                "redis package required for RedisPriorityQueue. "
                "Install with: pip install redis"
            )

        self._prefix = prefix
        self._client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
        # Verify connection
        self._client.ping()
        logger.info(f"Connected to Redis at {redis_url}")

    def _key(self, name: str) -> str:
        """Prefix a key name."""
        return f"{self._prefix}:{name}"

    def _queue_key(self) -> str:
        return self._key(KEY_QUEUE)

    def _data_key(self, conversation_id: str) -> str:
        return self._key(f"{KEY_DATA_PREFIX}{conversation_id}")

    def _meta_key(self) -> str:
        return self._key(KEY_META)

    # ── Core interface (matches PriorityQueue) ────────────────

    def add(self, scored) -> None:
        """Add or update a scored prospect."""
        from ..models.prospect import ScoredProspect
        if not isinstance(scored, ScoredProspect):
            raise TypeError("expected ScoredProspect")

        cid = str(scored.conversation_id)
        score = -(scored.priority_score or 0)  # Negate for descending order

        # Serialize prospect to JSON
        data = scored.model_dump(mode="json")
        # Convert datetime objects that model_dump may leave as strings
        data_json = json.dumps(data, default=str)

        pipe = self._client.pipeline()
        pipe.zadd(self._queue_key(), {cid: score})
        pipe.set(self._data_key(cid), data_json)
        pipe.execute()

    def pop_next(self):
        """Pop highest-priority non-breached prospect. SLA-breached first."""
        from ..models.prospect import ScoredProspect

        self._refresh_breaches()

        # Get all items sorted by score (highest priority = lowest score = first)
        items = self._client.zrange(self._queue_key(), 0, -1, withscores=True)
        if not items:
            return None

        # SLA-breached items first, then by priority
        breached_items = []
        normal_items = []
        for cid, score in items:
            data_json = self._client.get(self._data_key(cid))
            if not data_json:
                # Orphaned sorted set entry, clean up
                self._client.zrem(self._queue_key(), cid)
                continue
            prospect = ScoredProspect.model_validate_json(data_json)
            if prospect.sla_breached:
                breached_items.append((cid, prospect))
            else:
                normal_items.append((cid, prospect))

        # Pop from breached first, then normal
        to_pop = breached_items[0] if breached_items else (normal_items[0] if normal_items else None)
        if to_pop is None:
            return None

        cid, prospect = to_pop
        pipe = self._client.pipeline()
        pipe.zrem(self._queue_key(), cid)
        pipe.delete(self._data_key(cid))
        pipe.execute()

        return prospect

    def get_breached(self) -> list:
        """Return all SLA-breached prospects."""
        from ..models.prospect import ScoredProspect

        self._refresh_breaches()
        items = self._client.zrange(self._queue_key(), 0, -1, withscores=True)
        breached = []
        for cid, _score in items:
            data_json = self._client.get(self._data_key(cid))
            if not data_json:
                continue
            prospect = ScoredProspect.model_validate_json(data_json)
            if prospect.sla_breached:
                breached.append(prospect)
        return breached

    def list(self) -> list:
        """List all items sorted by priority (highest first)."""
        from ..models.prospect import ScoredProspect

        self._refresh_breaches()
        items = self._client.zrange(self._queue_key(), 0, -1, withscores=True)
        prospects = []
        for cid, _score in items:
            data_json = self._client.get(self._data_key(cid))
            if not data_json:
                continue
            prospect = ScoredProspect.model_validate_json(data_json)
            prospects.append(prospect)

        # Sort: breached first, then by priority descending
        prospects.sort(key=lambda s: (not s.sla_breached, -(s.priority_score or 0)))
        return prospects

    def size(self) -> int:
        return self._client.zcard(self._queue_key())

    def get_by_id(self, conversation_id: str):
        """Get a prospect by conversation ID."""
        from ..models.prospect import ScoredProspect

        data_json = self._client.get(self._data_key(str(conversation_id)))
        if not data_json:
            return None
        return ScoredProspect.model_validate_json(data_json)

    def remove(self, conversation_id: str) -> bool:
        """Remove a prospect by conversation ID."""
        cid = str(conversation_id)
        removed = self._client.zrem(self._queue_key(), cid)
        self._client.delete(self._data_key(cid))
        return removed > 0

    def increment_requeue(self, conversation_id: str) -> Optional[dict]:
        """Increment requeue counter for a prospect."""
        from ..models.prospect import ScoredProspect

        cid = str(conversation_id)
        data_json = self._client.get(self._data_key(cid))
        if not data_json:
            return None

        prospect = ScoredProspect.model_validate_json(data_json)
        prospect.times_requeued = (prospect.times_requeued or 0) + 1

        # Save updated prospect back
        updated_json = prospect.model_dump_json()
        score = -(prospect.priority_score or 0)

        pipe = self._client.pipeline()
        pipe.zadd(self._queue_key(), {cid: score})
        pipe.set(self._data_key(cid), updated_json)
        pipe.execute()

        return {
            "conversation_id": cid,
            "times_requeued": prospect.times_requeued,
        }

    def check_sla_breaches(self) -> list:
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
        items = self._client.zrange(self._queue_key(), 0, -1, withscores=True)
        if not items:
            return {"total_items": 0, "breached_count": 0, "avg_priority": 0.0}

        total = len(items)
        breached_count = 0
        total_priority = 0.0

        for cid, _score in items:
            data_json = self._client.get(self._data_key(cid))
            if not data_json:
                continue
            prospect_data = json.loads(data_json)
            priority = prospect_data.get("priority_score", 0)
            total_priority += priority
            if prospect_data.get("sla_breached", False):
                breached_count += 1

        return {
            "total_items": total,
            "breached_count": breached_count,
            "avg_priority": round(total_priority / total, 4) if total else 0.0,
        }

    def flush(self) -> int:
        """Delete ALL keys for this queue prefix. USE WITH CAUTION."""
        keys = []
        for k in self._client.scan_iter(f"{self._prefix}:*"):
            keys.append(k)
        if keys:
            return self._client.delete(*keys)
        return 0

    def ping(self) -> bool:
        """Check if Redis is reachable."""
        try:
            return self._client.ping()
        except Exception:
            return False

    # ── Internal ──────────────────────────────────────────────

    def _refresh_breaches(self) -> None:
        """Scan all prospects and mark SLA breaches."""
        from ..models.prospect import ScoredProspect

        now = datetime.utcnow()
        items = self._client.zrange(self._queue_key(), 0, -1, withscores=True)
        pipe = self._client.pipeline()
        updated = 0

        for cid, _score in items:
            data_json = self._client.get(self._data_key(cid))
            if not data_json:
                continue

            prospect = ScoredProspect.model_validate_json(data_json)
            if (
                prospect.sla_deadline
                and not prospect.sla_breached
                and now > prospect.sla_deadline
            ):
                prospect.sla_breached = True
                prospect.times_requeued = (prospect.times_requeued or 0) + 1
                prospect.status = "breached"

                # Update stored data
                pipe.set(self._data_key(cid), prospect.model_dump_json())
                updated += 1

        if updated:
            pipe.execute()
            logger.debug(f"Refreshed {updated} SLA breaches")
