"""FastAPI application for the AI Sales Follow-Up Agent."""

import hashlib
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import get_settings
from core.email_tracking import (
    EmailTracking,
    calculate_engagement_score,
    generate_tracking_link_url,
    generate_tracking_pixel_url,
)
from core.generation.prompt import generate_drafts, select_draft
from core.ingest.email import (
    add_suppression,
    fetch_emails,
    is_suppressed,
    parse_email_to_conversation,
    send_email,
)
from core.ingest.meeting import process_meeting_notes
from core.ingest.stt import process_call_audio, transcribe_audio
from core.intelligence.action_engine import determine_next_best_action
from core.intelligence.llm_manager import llm_manager
from core.intelligence.scorer import (
    calculate_recency_decay,
    score_prospect,
    validate_score_inputs,
)
from core.models.conversation import Conversation
from core.models.prospect import ScoredProspect
from core.queue import PriorityQueue, get_queue
from core.monitoring import metrics, record_request, record_queue_depth, record_sla_breach
from core.middleware import (
    RateLimiter,
    RateLimitMiddleware,
    WebSocketRateLimiter,
    get_api_key_verifier,
)

logger = logging.getLogger("API")
settings = get_settings()

# ── Rate Limiters ─────────────────────────────────────────────
http_rate_limiter = RateLimitMiddleware(
    default_max_requests=120,
    default_window_seconds=60,
    route_settings={
        "/pipeline/process": {"max_requests": 10, "window_seconds": 60},
        "/ingest/emails": {"max_requests": 5, "window_seconds": 60},
        "/ingest/call": {"max_requests": 10, "window_seconds": 60},
        "/drafts/generate": {"max_requests": 20, "window_seconds": 60},
        "/send/follow-up": {"max_requests": 15, "window_seconds": 60},
        "/sla/check": {"max_requests": 10, "window_seconds": 60},
    },
)
ws_rate_limiter = WebSocketRateLimiter(max_messages=30, window_seconds=60)
api_key_verifier = get_api_key_verifier()

app = FastAPI(
    title="AI Sales Follow-Up Agent",
    description="LeadSync - autonomous sales follow-up platform with intelligent LLM fallback",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Lifespan: initialize database + SLA checker ──────────────
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    try:
        from core.database import init_db
        init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.warning(f"Database init failed (non-fatal): {e}")
    try:
        from core.alerts import get_sla_checker
        checker = get_sla_checker()
        checker.start()
        logger.info("SLA breach checker started")
    except Exception as e:
        logger.warning(f"SLA checker start failed (non-fatal): {e}")
    yield
    # shutdown
    try:
        from core.alerts import get_sla_checker
        get_sla_checker().stop()
    except Exception:
        pass

# attach lifespan to app (FastAPI >=0.109 supports lifespan param; keep on_event for compat)
app.router.lifespan_context = lifespan
# keep deprecated hooks for backward compat with older tests
@app.on_event("startup")
async def startup_event():
    async with lifespan(app):
        pass
@app.on_event("shutdown")
async def shutdown_event():
    pass


# ── Request metrics middleware ────────────────────────────────
import time as _time
from starlette.middleware.base import BaseHTTPMiddleware


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_handler):
        start = _time.time()
        response = await call_handler(request)
        duration = _time.time() - start
        record_request(
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration=duration,
        )
        return response


app.add_middleware(MetricsMiddleware)


# ── Rate Limit Middleware ──────────────────────────────────────
class RateLimitHTTPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_handler):
        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        path = request.url.path

        allowed, info = http_rate_limiter.check(path, client_ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "message": f"Rate limit exceeded. Try again in {info['retry_after']}s.",
                    **info,
                },
                headers={
                    "X-RateLimit-Limit": str(info["limit"]),
                    "X-RateLimit-Remaining": str(info["remaining"]),
                    "X-RateLimit-Reset": info["reset_at"],
                    "Retry-After": str(info["retry_after"]),
                },
            )

        response = await call_handler(request)
        # Add rate limit headers to successful responses
        if hasattr(response, "headers"):
            response.headers["X-RateLimit-Limit"] = str(info.get("limit", 120))
            response.headers["X-RateLimit-Remaining"] = str(info.get("remaining", ""))
        return response


app.add_middleware(RateLimitHTTPMiddleware)


# ========== Health & System ==========


@app.get("/", tags=["root"])
async def root() -> Dict[str, Any]:
    return {
        "message": "AI Sales Follow-Up Agent API",
        "version": "1.0.0",
        "docs": "/docs",
        "status": "operational",
    }


@app.get("/health", tags=["system"])
async def health_check() -> Dict[str, Any]:
    queue = get_queue()
    llm_available = False
    try:
        llm_available = llm_manager.is_local_available()
    except Exception:
        pass
    llm_status = "available" if llm_available else "cloud-only"
    try:
        llm_health_data = llm_manager.get_health_report()
    except Exception:
        llm_health_data = {}

    return {
        "status": "healthy",
        "version": "1.0.0",
        "queue_depth": queue.size(),
        "llm_provider": settings.llm_provider,
        "llm_status": llm_status,
        "llm_health": llm_health_data,
        "imap_configured": bool(settings.imap_username),
        "smtp_configured": bool(settings.smtp_username),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/metrics", tags=["system"])
async def prometheus_metrics() -> Response:
    """Prometheus metrics endpoint. Returns text in Prometheus exposition format."""
    # Update gauges before export
    queue = get_queue()
    stats = queue.get_queue_stats()
    record_queue_depth(stats["total_items"])
    record_sla_breach(stats["breached_count"])
    return Response(content=metrics.export(), media_type="text/plain; version=0.0.4; charset=utf-8")


# ========== Real-time WebSocket & SSE ==========

from core.realtime import ws_manager, event_bus, format_sse_event
import asyncio


@app.websocket("/ws/queue")
async def websocket_queue(
    websocket: WebSocket,
    api_key: Optional[str] = Query(None),
):
    """
    WebSocket endpoint for real-time queue updates.

    Authentication: Pass `?api_key=xxx` as query param.
    Rate limited: 30 messages/minute per connection.

    Clients receive JSON events:
    {
        "type": "queue:added" | "queue:popped" | "queue:removed" | "queue:breach",
        "data": { "conversation_id": ..., "priority_score": ..., ... },
        "timestamp": "..."
    }

    Also sends periodic heartbeat (every 30s) to keep connection alive.
    """
    # ── API Key Authentication ──────────────────────────────
    auth_metadata = None
    if api_key:
        auth_metadata = api_key_verifier.verify(api_key)
        if auth_metadata is None:
            await websocket.accept()
            await websocket.send_json({
                "type": "error",
                "data": {"message": "Invalid API key", "code": 4003},
            })
            await websocket.close(code=4003, reason="Invalid API key")
            return
    else:
        # No API key provided — accept but mark as unauthenticated
        # (allows anonymous read-only access if configured)
        auth_metadata = {"name": "anonymous", "role": "viewer"}
        logger.debug("WebSocket connected without API key (anonymous)")

    # ── Connect ─────────────────────────────────────────────
    await ws_manager.connect(websocket)

    # Send auth confirmation
    await ws_manager._send_to(websocket, {
        "type": "auth",
        "data": {
            "authenticated": api_key is not None,
            "user": auth_metadata.get("name", "anonymous"),
            "role": auth_metadata.get("role", "viewer"),
            "rate_limit": ws_rate_limiter.max_messages,
            "rate_window": ws_rate_limiter.window_seconds,
        },
        "timestamp": datetime.utcnow().isoformat(),
    })

    try:
        while True:
            # Wait for client messages (ping/pong or commands)
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)

                # ── Rate Limit Check ──────────────────────
                allowed, rate_info = ws_rate_limiter.is_allowed(websocket)
                if not allowed:
                    await ws_manager._send_to(websocket, {
                        "type": "error",
                        "data": {
                            "message": f"Rate limit exceeded. {rate_info['retry_after']}s until reset.",
                            "code": 429,
                            **rate_info,
                        },
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                    continue

                # ── Command Handling ──────────────────────
                if data == "ping":
                    await ws_manager._send_to(websocket, {
                        "type": "pong",
                        "data": {
                            "connections": ws_manager.connection_count,
                            "rate_remaining": rate_info.get("remaining"),
                        },
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                elif data == "stats":
                    q = get_queue()
                    stats = q.get_queue_stats()
                    await ws_manager._send_to(websocket, {
                        "type": "stats",
                        "data": stats,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                elif data == "whoami":
                    await ws_manager._send_to(websocket, {
                        "type": "whoami",
                        "data": auth_metadata,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                else:
                    # Unknown command
                    await ws_manager._send_to(websocket, {
                        "type": "error",
                        "data": {"message": f"Unknown command: {data}. Use 'ping', 'stats', or 'whoami'."},
                        "timestamp": datetime.utcnow().isoformat(),
                    })

            except asyncio.TimeoutError:
                # Send heartbeat
                try:
                    await ws_manager._send_to(websocket, {
                        "type": "heartbeat",
                        "data": {"connections": ws_manager.connection_count},
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
    finally:
        ws_rate_limiter.remove(websocket)
        await ws_manager.disconnect(websocket)


@app.get("/events/queue", tags=["realtime"])
async def sse_queue_events():
    """
    Server-Sent Events endpoint for queue updates.

    Simpler than WebSocket — works with any HTTP client.
    Returns a stream of events:
        event: queue:added
        data: {"type": "queue:added", "data": {...}, "timestamp": "..."}

    Keep-alive comment sent every 15s.
    """
    import json

    async def event_generator():
        queue = asyncio.Queue()

        def on_event(event):
            try:
                queue.put_nowait(event)
            except Exception:
                pass

        # Subscribe to all queue events
        event_bus.on("queue:added", on_event)
        event_bus.on("queue:popped", on_event)
        event_bus.on("queue:removed", on_event)
        event_bus.on("queue:breach", on_event)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield format_sse_event(event["type"], event.get("data", {}))
                except asyncio.TimeoutError:
                    # Send keep-alive comment
                    yield f": heartbeat {datetime.utcnow().isoformat()}\n\n"
        finally:
            event_bus.off("queue:added", on_event)
            event_bus.off("queue:popped", on_event)
            event_bus.off("queue:removed", on_event)
            event_bus.off("queue:breach", on_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/ws/stats", tags=["realtime"])
async def websocket_stats() -> Dict[str, Any]:
    """WebSocket manager stats (for monitoring)."""
    return {
        "active_connections": ws_manager.connection_count,
        "total_broadcasts": ws_manager.broadcast_count,
        "recent_events": len(event_bus.get_recent(100)),
    }


@app.get("/config", tags=["system"])
async def get_config() -> Dict[str, Any]:
    config = settings.get_all_config() if hasattr(settings, "get_all_config") else {}
    return {"config": config, "provided_at": datetime.utcnow().isoformat()}


@app.get("/rate-limit/status", tags=["system"])
async def rate_limit_status() -> Dict[str, Any]:
    """Get rate limiter configuration and current usage."""
    return {
        "http": {
            "default_limit": http_rate_limiter._default.max_requests,
            "window_seconds": http_rate_limiter._default.window_seconds,
            "route_overrides": {
                path: {
                    "max_requests": cfg["max_requests"],
                    "window_seconds": cfg["window_seconds"],
                }
                for path, cfg in http_rate_limiter._route_settings.items()
            },
        },
        "websocket": {
            "max_messages": ws_rate_limiter.max_messages,
            "window_seconds": ws_rate_limiter.window_seconds,
        },
    }


# ========== Ingestion ==========


@app.post("/ingest/emails", tags=["ingestion"])
async def ingest_emails(
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    mailbox: str = "INBOX",
    since_days: Optional[int] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    host = imap_host or settings.imap_host
    port = imap_port or settings.imap_port
    user = username or settings.imap_username
    pwd = password or settings.imap_password

    if not user or not pwd:
        raise HTTPException(status_code=400, detail="IMAP username and password required")

    try:
        conversations = fetch_emails(
            imap_host=host,
            imap_port=port,
            username=user,
            password=pwd,
            mailbox=mailbox,
            since_days=since_days,
            limit=limit,
        )
        queue = get_queue()
        for conv in conversations:
            try:
                scored = score_prospect(conversation=conv)
                queue.add(scored)
            except Exception as e:
                logger.warning(f"Failed to score conversation {conv.id}: {e}")

        return {
            "message": f"Fetched and queued {len(conversations)} conversations",
            "conversation_count": len(conversations),
            "queue_size": queue.size(),
            "status": "completed",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email ingestion failed: {e}")


@app.post("/ingest/email", tags=["ingestion"])
async def ingest_single_email(email_data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a single email dict into a Conversation and queue it."""
    try:
        # Check dedup before processing
        from core.dedup import get_dedup_store
        dedup = get_dedup_store()
        msg_id = str(email_data.get("message_id") or email_data.get("Message-ID") or "")
        sender = str(email_data.get("from") or email_data.get("sender") or "")
        subject = str(email_data.get("subject") or "")
        body = str(email_data.get("body") or "")

        if dedup.is_duplicate(message_id=msg_id or None, sender=sender, subject=subject, body=body):
            return {
                "message": "Duplicate email — skipped",
                "status": "duplicate",
                "timestamp": datetime.utcnow().isoformat(),
            }

        conv = parse_email_to_conversation(email_data)
        scored = score_prospect(conversation=conv)
        queue = get_queue()
        queue.add(scored)
        return {
            "message": "Email parsed and queued",
            "conversation_id": conv.id,
            "priority_score": scored.priority_score,
            "status": "queued",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email processing failed: {e}")


@app.post("/ingest/call", tags=["ingestion"])
async def ingest_call(
    audio_path: str = Form(None),
    model_size: str = Form("base"),
    language: Optional[str] = Form(None),
    prospect_name: Optional[str] = Form(None),
    file: Optional[Any] = None,
) -> Dict[str, Any]:
    # Secure handling: if file upload provided use it, else validate audio_path to prevent traversal
    import os as _os
    from fastapi import UploadFile, File
    # If audio_path contains traversal or absolute path outside allowed dir, reject
    if audio_path:
        # disallow .., absolute paths, and non-existent
        if ".." in audio_path or _os.path.isabs(audio_path):
            raise HTTPException(status_code=400, detail="Invalid audio_path")
        # restrict to data/ or temp allowed dirs or existing file
        if not _os.path.exists(audio_path):
            raise HTTPException(status_code=404, detail=f"Audio file not found: {audio_path}")
    else:
        raise HTTPException(status_code=400, detail="audio_path required (or upload file)")
    try:
        conv = process_call_audio(
            audio_path=audio_path,
            model_size=model_size,
            language=language,
            prospect_name=prospect_name,
        )
        scored = score_prospect(conversation=conv)
        queue = get_queue()
        queue.add(scored)
        return {
            "message": "Call processed and queued",
            "conversation_id": conv.id,
            "source": conv.source,
            "urgency": conv.urgency,
            "sentiment": conv.sentiment,
            "priority_score": scored.priority_score,
            "transcript_preview": conv.raw_text[:200] + "..." if len(conv.raw_text) > 200 else conv.raw_text,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Call processing failed: {e}")


@app.post("/ingest/meeting", tags=["ingestion"])
async def ingest_meeting(
    notes: str = Form(...),
    source: str = Form("meeting"),
) -> Dict[str, Any]:
    try:
        conv = process_meeting_notes(notes=notes, source=source)
        scored = score_prospect(conversation=conv)
        queue = get_queue()
        queue.add(scored)
        return {
            "message": "Meeting notes processed and queued",
            "conversation_id": conv.id,
            "commitments": conv.commitments,
            "urgency": conv.urgency,
            "priority_score": scored.priority_score,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Meeting processing failed: {e}")


# ========== Scoring ==========


@app.post("/score", tags=["intelligence"])
async def score_conversation(conversation: Conversation) -> Dict[str, Any]:
    try:
        scored = score_prospect(conversation=conversation)
        action = determine_next_best_action(conversation=conversation)
        return {
            "conversation_id": scored.conversation_id,
            "priority_score": scored.priority_score,
            "recency_days": scored.recency_days,
            "engagement_probability": scored.engagement_probability,
            "deal_value_normalized": scored.deal_value_normalized,
            "urgency_score": scored.urgency_score,
            "sla_deadline": scored.sla_deadline.isoformat(),
            "sla_breached": scored.sla_breached,
            "next_action": action["action_type"],
            "timing": action["timing_recommendation"],
            "rationale": action["rationale"],
            "escalation": action["escalation_level"],
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scoring failed: {e}")


@app.get("/score/decay", tags=["intelligence"])
async def get_decay(days: float = 0.0) -> Dict[str, Any]:
    return {"days": days, "decay": calculate_recency_decay(days)}


# ========== Action Engine ==========


@app.post("/action/determine", tags=["intelligence"])
async def determine_action(
    conversation: Conversation,
    rep_workload: int = Query(0, ge=0),
    rep_closing_deals: int = Query(0, ge=0),
) -> Dict[str, Any]:
    try:
        context = {"rep_workload": rep_workload, "rep_closing_deals": rep_closing_deals}
        action = determine_next_best_action(conversation=conversation, pipeline_context=context)
        return {**action, "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Action determination failed: {e}")


# ========== Draft Generation ==========


@app.post("/drafts/generate", tags=["generation"])
async def generate_drafts_endpoint(
    conversation: Conversation,
    prospect_name: str = Query(...),
    company: str = Query(""),
    role: str = Query(""),
    pain_points: List[str] = Query([]),
    followup_count: int = Query(0, ge=0),
    urgency_level: str = Query("medium"),
    use_llm: bool = Query(True),
) -> Dict[str, Any]:
    try:
        drafts = generate_drafts(
            conversation=conversation,
            prospect_name=prospect_name,
            company=company,
            role=role,
            pain_points=pain_points,
            followup_count=followup_count,
            urgency_level=urgency_level,
            use_llm=use_llm,
        )
        selected = select_draft(drafts=drafts, urgency_level=urgency_level)
        return {
            "message": "3 follow-up drafts generated",
            "variants": drafts,
            "selected_variant": selected,
            "urgency_level": urgency_level,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Draft generation failed: {e}")


# ========== Autonomous Pipeline ==========


@app.post("/pipeline/process", tags=["pipeline"])
async def process_conversation_pipeline(
    conversation: Conversation,
    prospect_name: str = Query("Prospect"),
    company: str = Query(""),
    role: str = Query(""),
    pain_points: List[str] = Query([]),
    deal_value: Optional[float] = Query(None),
) -> Dict[str, Any]:
    """Full autonomous pipeline: Score -> Action -> Generate -> Queue."""
    try:
        scored = score_prospect(conversation=conversation, deal_value=deal_value)
        action = determine_next_best_action(conversation=conversation)

        drafts = {}
        if action["action_type"] in ("close", "re-engage", "nurture"):
            drafts = generate_drafts(
                conversation=conversation,
                prospect_name=prospect_name,
                company=company,
                role=role,
                pain_points=pain_points,
                followup_count=0,
                urgency_level=conversation.urgency or "low",
            )

        queue = get_queue()
        queue.add(scored)

        selected = select_draft(drafts, urgency_level=conversation.urgency or "medium") if drafts else None

        return {
            "status": "success",
            "conversation_id": scored.conversation_id,
            "priority_score": scored.priority_score,
            "sla_deadline": scored.sla_deadline.isoformat(),
            "action": action,
            "drafts": drafts,
            "selected_variant": selected,
            "queue_size": queue.size(),
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}")


# ========== Queue ==========


@app.get("/queue/stats", tags=["queue"])
async def queue_stats() -> Dict[str, Any]:
    return get_queue().get_queue_stats()


@app.get("/queue/list", tags=["queue"])
async def queue_list() -> Dict[str, Any]:
    items = get_queue().list()
    return {
        "count": len(items),
        "items": [
            {
                "conversation_id": s.conversation_id,
                "priority_score": s.priority_score,
                "status": s.status,
                "sla_breached": s.sla_breached,
                "urgency": s.conversation.urgency if s.conversation else "unknown",
            }
            for s in items
        ],
    }


@app.post("/queue/check-sla", tags=["queue"])
async def check_sla_breaches() -> Dict[str, Any]:
    breaches = get_queue().check_sla_breaches()
    return {
        "breach_count": len(breaches),
        "breached_prospects": breaches,
        "checked_at": datetime.utcnow().isoformat(),
    }


@app.post("/queue/pop", tags=["queue"])
async def pop_next() -> Dict[str, Any]:
    item = get_queue().pop_next()
    if item is None:
        return {"message": "Queue is empty", "item": None}
    return {
        "message": "Popped next prospect",
        "conversation_id": item.conversation_id,
        "priority_score": item.priority_score,
        "status": item.status,
    }


@app.post("/queue/requeue/{conversation_id}", tags=["queue"])
async def requeue_prospect(conversation_id: str) -> Dict[str, Any]:
    result = get_queue().increment_requeue(conversation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Prospect not found in queue")
    return {"message": "Prospect re-queued", **result, "timestamp": datetime.utcnow().isoformat()}


@app.delete("/queue/{conversation_id}", tags=["queue"])
async def remove_from_queue(conversation_id: str) -> Dict[str, Any]:
    removed = get_queue().remove(conversation_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return {"message": "Removed from queue", "conversation_id": conversation_id}


# ========== Email Tracking ==========


@app.get("/tracking/pixel/{prospect_id}", tags=["tracking"])
async def tracking_pixel(prospect_id: str) -> Response:
    pixel_data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\x0a\x00\x3b"
    return Response(content=pixel_data, media_type="image/gif")


@app.post("/tracking/click", tags=["tracking"])
async def track_click(
    prospect_id: str = Form(...),
    link_url: str = Form(...),
) -> Dict[str, Any]:
    return {
        "status": "tracked",
        "prospect_id": prospect_id,
        "link_url": link_url,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ========== Follow-up Sending ==========


@app.post("/send/follow-up", tags=["send"])
async def send_follow_up(
    prospect_id: str = Form(...),
    to_address: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    variant: str = Form("direct"),
    include_tracking: bool = Form(True),
) -> Dict[str, Any]:
    # GDPR/CCPA: block suppressed
    if is_suppressed(to_address):
        raise HTTPException(status_code=403, detail=f"Recipient {to_address} is on suppression list (GDPR/CCPA)")
    tracking_url = None
    if include_tracking:
        tracking_url = generate_tracking_pixel_url(
            settings.tracking_base_url,
            prospect_id,
            hashlib.md5(to_address.encode()).hexdigest()[:12],
        )

    result = send_email(
        to_address=to_address,
        subject=subject,
        body=body,
        include_tracking_pixel=include_tracking,
        tracking_pixel_url=tracking_url,
    )

    # audit
    try:
        from core.database.audit import audit
        audit.log("email:sent" if result.get("status")=="sent" else "email:failed", entity_type="prospect", entity_id=prospect_id, details={"to": to_address, "variant": variant, "status": result.get("status")})
    except Exception:
        pass
    return {**result, "prospect_id": prospect_id, "variant": variant}


# ========== Suppressions ==========


@app.post("/suppressions/add", tags=["compliance"])
async def add_suppression_endpoint(email_address: str = Form(...)) -> Dict[str, Any]:
    if not email_address or "@" not in email_address:
        raise HTTPException(status_code=400, detail="Invalid email address")
    success = add_suppression(email_address)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to add suppression")
    return {"message": f"Email {email_address} added to suppression list", "timestamp": datetime.utcnow().isoformat()}


@app.get("/suppressions/check/{email_address}", tags=["compliance"])
async def check_suppression(email_address: str) -> Dict[str, Any]:
    suppressed = is_suppressed(email_address)
    return {"email_address": email_address, "is_suppressed": suppressed, "checked_at": datetime.utcnow().isoformat()}


# ========== LLM Health ==========


@app.get("/llm/health", tags=["llm"])
async def llm_health() -> Dict[str, Any]:
    local_available = False
    try:
        local_available = llm_manager.is_local_available()
    except Exception:
        pass
    return {
        "local_available": local_available,
        "health_report": llm_manager.get_health_report(),
        "configured_provider": settings.llm_provider,
    }


@app.get("/llm/local-models", tags=["llm"])
async def local_models() -> Dict[str, Any]:
    models = llm_manager._get_ollama_models()
    return {"available": models, "count": len(models), "ollama_host": settings.ollama_host}


# ========== Database: History & Audit ==========


@app.get("/history/conversations", tags=["database"])
async def list_conversations(
    source: Optional[str] = Query(None),
    urgency: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """List stored conversations with optional filters."""
    try:
        from core.database import get_db
        from core.database.models import ConversationRecord
        from sqlalchemy import desc

        with get_db() as db:
            query = db.query(ConversationRecord)
            if source:
                query = query.filter(ConversationRecord.source == source)
            if urgency:
                query = query.filter(ConversationRecord.urgency == urgency)
            total = query.count()
            items = (
                query.order_by(desc(ConversationRecord.ingested_at))
                .offset(offset)
                .limit(limit)
                .all()
            )
            return {
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": [
                    {
                        "id": c.id,
                        "source": c.source,
                        "participants": c.participants,
                        "sentiment": c.sentiment,
                        "urgency": c.urgency,
                        "deal_size": c.deal_size,
                        "commitments": c.commitments,
                        "conversation_date": c.conversation_date.isoformat() if c.conversation_date else None,
                        "ingested_at": c.ingested_at.isoformat() if c.ingested_at else None,
                    }
                    for c in items
                ],
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list conversations: {e}")


@app.get("/history/conversations/{conversation_id}", tags=["database"])
async def get_conversation(conversation_id: str) -> Dict[str, Any]:
    """Get a single conversation by ID with full details."""
    try:
        from core.database import get_db
        from core.database.models import ConversationRecord

        with get_db() as db:
            conv = db.query(ConversationRecord).filter(ConversationRecord.id == conversation_id).first()
            if not conv:
                raise HTTPException(status_code=404, detail="Conversation not found")
            return {
                "id": conv.id,
                "source": conv.source,
                "participants": conv.participants,
                "raw_text": conv.raw_text,
                "commitments": conv.commitments,
                "entities": conv.entities,
                "sentiment": conv.sentiment,
                "deal_size": conv.deal_size,
                "urgency": conv.urgency,
                "tags": conv.tags,
                "conversation_date": conv.conversation_date.isoformat() if conv.conversation_date else None,
                "ingested_at": conv.ingested_at.isoformat() if conv.ingested_at else None,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get conversation: {e}")


@app.post("/history/conversations", tags=["database"])
async def store_conversation(
    conversation: Conversation,
    tags: List[str] = Query([]),
) -> Dict[str, Any]:
    """Store a conversation to the database."""
    try:
        from core.database import get_db
        from core.database.models import ConversationRecord

        with get_db() as db:
            record = ConversationRecord(
                id=conversation.id,
                source=conversation.source,
                participants=conversation.participants,
                raw_text=conversation.raw_text,
                commitments=conversation.commitments,
                entities=conversation.entities.model_dump() if conversation.entities else {},
                sentiment=conversation.sentiment,
                deal_size=conversation.deal_size,
                urgency=conversation.urgency,
                conversation_date=conversation.date,
                tags=tags,
            )
            db.add(record)

            # Also audit
            from core.database.audit import audit
            audit.log(
                "conversation:stored",
                entity_type="conversation",
                entity_id=conversation.id,
                details={"source": conversation.source, "urgency": conversation.urgency},
            )

            return {
                "message": "Conversation stored",
                "id": record.id,
                "timestamp": datetime.utcnow().isoformat(),
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store conversation: {e}")


@app.get("/audit", tags=["database"])
async def audit_log(
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """Query the audit trail."""
    try:
        from core.database.audit import audit as audit_logger

        entries = audit_logger.query(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit,
        )
        return {
            "count": len(entries),
            "entries": entries,
            "stats": audit_logger.get_stats(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query audit log: {e}")


@app.get("/audit/stats", tags=["database"])
async def audit_stats() -> Dict[str, Any]:
    """Get audit logging statistics."""
    from core.database.audit import audit as audit_logger
    return audit_logger.get_stats()


# ========== SLA Breach Checker ==========


@app.get("/sla/status", tags=["sla"])
async def sla_checker_status() -> Dict[str, Any]:
    """Get SLA breach checker status."""
    from core.alerts import get_sla_checker
    return get_sla_checker().get_status()


@app.post("/sla/check", tags=["sla"])
async def sla_check_now() -> Dict[str, Any]:
    """Run an immediate SLA breach check."""
    from core.alerts import get_sla_checker
    return get_sla_checker().check_now()


@app.post("/sla/start", tags=["sla"])
async def sla_start() -> Dict[str, Any]:
    """Start the SLA breach checker background task."""
    from core.alerts import get_sla_checker
    checker = get_sla_checker()
    checker.start()
    return {"message": "SLA checker started", "status": checker.get_status()}


@app.post("/sla/stop", tags=["sla"])
async def sla_stop() -> Dict[str, Any]:
    """Stop the SLA breach checker background task."""
    from core.alerts import get_sla_checker
    checker = get_sla_checker()
    checker.stop()
    return {"message": "SLA checker stopped", "status": checker.get_status()}


@app.post("/sla/test-alert", tags=["sla"])
async def sla_test_alert() -> Dict[str, Any]:
    """Send a test alert through all configured channels."""
    from core.alerts import get_sla_checker, build_breach_alert
    from core.models.conversation import Conversation
    from core.models.prospect import ScoredProspect

    # Create a fake breached prospect for testing
    conv = Conversation(
        source="email",
        participants=[{"name": "Test Prospect", "email": "test@example.com"}],
        raw_text="This is a test alert.",
        urgency="high",
        deal_size=75000.0,
        commitments=["send proposal"],
    )
    prospect = ScoredProspect(
        conversation_id="test-alert-001",
        priority_score=0.92,
        conversation=conv,
        sla_deadline=datetime.utcnow(),
        sla_breached=True,
        times_requeued=4,
    )

    alert = build_breach_alert(prospect)
    checker = get_sla_checker()
    result = checker.alert_manager.send_alert(alert, "test-alert-001")

    return {
        "message": "Test alert sent",
        "alert": alert,
        "result": result,
    }


@app.get("/webhooks/channels", tags=["webhooks"])
async def webhook_channels() -> Dict[str, Any]:
    """List all configured webhook alert channels."""
    import os
    channels = []

    channel_config = [
        ("telegram", "TELEGRAM_BOT_TOKEN", "Telegram Bot API"),
        ("email", "ALERT_EMAIL", "SMTP Email"),
        ("slack", "SLACK_WEBHOOK_URL", "Slack Incoming Webhook"),
        ("discord", "DISCORD_WEBHOOK_URL", "Discord Webhook"),
        ("teams", "TEAMS_WEBHOOK_URL", "Microsoft Teams"),
        ("pagerduty", "PAGERDUTY_INTEGRATION_KEY", "PagerDuty Events API v2"),
        ("opsgenie", "OPSGENIE_API_KEY", "Opsgenie Alert API"),
    ]

    for slug, env_var, description in channel_config:
        configured = bool(os.environ.get(env_var, ""))
        channels.append({
            "slug": slug,
            "description": description,
            "env_var": env_var,
            "configured": configured,
        })

    return {
        "channels": channels,
        "total_configured": sum(1 for c in channels if c["configured"]),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/webhooks/test", tags=["webhooks"])
async def webhook_test(
    channel: Optional[str] = Query(None, description="Channel to test (None = all)"),
    urgency: str = Query("high", description="Test urgency level"),
) -> Dict[str, Any]:
    """Send a test alert to one or all configured channels."""
    import os
    from core.alerts import (
        get_sla_checker, build_breach_alert,
        TelegramSender, EmailSender, SlackSender, DiscordSender,
        TeamsSender, PagerDutySender, OpsgenieSender,
    )
    from core.models.conversation import Conversation
    from core.models.prospect import ScoredProspect
    from core.config import get_settings

    # Build test prospect
    conv = Conversation(
        source="email",
        participants=[{"name": "Webhook Test", "email": "webhook-test@example.com"}],
        raw_text="This is a webhook test from the Sales Follow-Up Agent.",
        urgency=urgency,
        deal_size=50000.0,
        commitments=["test webhook integration"],
    )
    prospect = ScoredProspect(
        conversation_id=f"wh-test-{int(time.time())}",
        priority_score=0.88,
        conversation=conv,
        sla_deadline=datetime.utcnow(),
        sla_breached=True,
        times_requeued=0,
    )
    alert = build_breach_alert(prospect)

    results = {}

    def _test_channel(slug: str, send_fn) -> Dict[str, Any]:
        try:
            start = time.time()
            success = send_fn(alert)
            elapsed_ms = round((time.time() - start) * 1000)
            return {
                "success": success,
                "latency_ms": elapsed_ms,
                "status": "sent" if success else "failed",
            }
        except Exception as e:
            return {"success": False, "error": str(e), "status": "error"}

    settings = get_settings()

    # Test specific channel or all
    channels_to_test = []
    if channel:
        channels_to_test.append(channel)
    else:
        channels_to_test = ["telegram", "email", "slack", "discord", "teams", "pagerduty", "opsgenie"]

    for ch in channels_to_test:
        if ch == "telegram":
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
            if token and chat_id:
                sender = TelegramSender(token, chat_id)
                results["telegram"] = _test_channel("telegram", sender.send_breach_alert)

        elif ch == "email":
            alert_email = os.environ.get("ALERT_EMAIL", "")
            if alert_email and settings.smtp_username:
                sender = EmailSender(
                    to_address=alert_email,
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    smtp_username=settings.smtp_username,
                    smtp_password=settings.smtp_password,
                )
                results["email"] = _test_channel("email", sender.send_breach_alert)

        elif ch == "slack":
            webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
            if webhook:
                sender = SlackSender(webhook, channel=os.environ.get("SLACK_CHANNEL"))
                results["slack"] = _test_channel("slack", sender.send_breach_alert)

        elif ch == "discord":
            webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
            if webhook:
                sender = DiscordSender(webhook)
                results["discord"] = _test_channel("discord", sender.send_breach_alert)

        elif ch == "teams":
            webhook = os.environ.get("TEAMS_WEBHOOK_URL", "")
            if webhook:
                sender = TeamsSender(webhook)
                results["teams"] = _test_channel("teams", sender.send_breach_alert)

        elif ch == "pagerduty":
            key = os.environ.get("PAGERDUTY_INTEGRATION_KEY", "")
            if key:
                sender = PagerDutySender(key, from_email=os.environ.get("ALERT_EMAIL"))
                results["pagerduty"] = _test_channel("pagerduty", sender.send_breach_alert)

        elif ch == "opsgenie":
            key = os.environ.get("OPSGENIE_API_KEY", "")
            if key:
                sender = OpsgenieSender(
                    key,
                    team=os.environ.get("OPSGENIE_TEAM"),
                    priority=os.environ.get("OPSGENIE_PRIORITY", "P2"),
                )
                results["opsgenie"] = _test_channel("opsgenie", sender.send_breach_alert)

        if ch not in results:
            results[ch] = {"success": False, "status": "not_configured"}

    # Summary
    sent = sum(1 for r in results.values() if r.get("status") == "sent")
    failed = sum(1 for r in results.values() if r.get("status") in ("failed", "error"))
    skipped = sum(1 for r in results.values() if r.get("status") == "not_configured")

    return {
        "message": f"Test complete: {sent} sent, {failed} failed, {skipped} not configured",
        "urgency": urgency,
        "results": results,
        "summary": {"sent": sent, "failed": failed, "not_configured": skipped},
        "timestamp": datetime.utcnow().isoformat(),
    }


# ========== Webhook Inspector ==========


@app.get("/webhooks/inspector/entries", tags=["webhooks"])
async def inspector_entries(
    channel: Optional[str] = Query(None, description="Filter by channel name"),
    channel_type: Optional[str] = Query(None, description="Filter by channel type"),
    success: Optional[bool] = Query(None, description="Filter by success status"),
    since: Optional[float] = Query(None, description="Unix timestamp - entries after this time"),
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    """Get captured webhook payloads with optional filtering."""
    from core.alerts.inspector import get_inspector
    inspector = get_inspector()
    entries = inspector.get_entries(
        channel=channel,
        channel_type=channel_type,
        success=success,
        since=since,
        limit=limit,
    )
    return {
        "count": len(entries),
        "entries": entries,
    }


@app.get("/webhooks/inspector/entry/{entry_id}", tags=["webhooks"])
async def inspector_entry_detail(entry_id: int) -> Dict[str, Any]:
    """Get a single captured payload by ID."""
    from core.alerts.inspector import get_inspector
    inspector = get_inspector()
    entry = inspector.get_entry(entry_id)
    if entry is None:
        return {"error": f"Entry {entry_id} not found"}
    return entry


@app.get("/webhooks/inspector/stats", tags=["webhooks"])
async def inspector_stats() -> Dict[str, Any]:
    """Get inspector statistics."""
    from core.alerts.inspector import get_inspector
    return get_inspector().get_stats()


@app.get("/webhooks/inspector/channels", tags=["webhooks"])
async def inspector_channels() -> Dict[str, Any]:
    """Get list of channels that have sent payloads."""
    from core.alerts.inspector import get_inspector
    channels = get_inspector().get_channel_list()
    return {"count": len(channels), "channels": channels}


@app.post("/webhooks/inspector/capture", tags=["webhooks"])
async def inspector_capture_manual(
    channel: str = Query(..., description="Channel name"),
    channel_type: str = Query("manual", description="Channel type"),
    success: bool = Query(True, description="Success status"),
    latency_ms: int = Query(0, description="Latency in ms"),
    error: Optional[str] = Query(None, description="Error message"),
) -> Dict[str, Any]:
    """Manually capture a payload (for testing external integrations)."""
    from core.alerts.inspector import get_inspector
    inspector = get_inspector()
    entry = inspector.capture_manual(
        channel=channel,
        payload={"manual": True, "channel": channel},
        channel_type=channel_type,
        success=success,
        latency_ms=latency_ms,
        error=error,
    )
    return {"captured": True, "entry": entry}


@app.post("/webhooks/inspector/clear", tags=["webhooks"])
async def inspector_clear() -> Dict[str, Any]:
    """Clear all captured payloads."""
    from core.alerts.inspector import get_inspector
    cleared = get_inspector().clear()
    return {"cleared": cleared, "message": f"Cleared {cleared} entry(ies)"}


@app.get("/webhooks/inspector/export", tags=["webhooks"])
async def inspector_export(
    limit: int = Query(1000, ge=1, le=10000),
) -> Response:
    """Export captured payloads as JSON file."""
    from core.alerts.inspector import get_inspector
    from fastapi.responses import Response
    inspector = get_inspector()
    json_str = inspector.export_json(limit=limit)
    return Response(
        content=json_str,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=webhook-inspector-export.json"},
    )


@app.post("/webhooks/inspector/toggle", tags=["webhooks"])
async def inspector_toggle(
    enabled: bool = Query(..., description="Enable or disable inspector"),
) -> Dict[str, Any]:
    """Enable or disable the webhook inspector."""
    from core.alerts.inspector import get_inspector
    inspector = get_inspector()
    inspector.enabled = enabled
    return {"enabled": enabled, "message": f"Inspector {'enabled' if enabled else 'disabled'}"}


# ========== Backup & Restore ==========


@app.get("/backup/status", tags=["backup"])
async def backup_status() -> Dict[str, Any]:
    """Show database and backup status."""
    from scripts.db_backup import show_status
    return show_status()


@app.post("/backup/run", tags=["backup"])
async def backup_run(
    output_dir: str = Query("backups"),
    compress: bool = Query(True),
    rotate: int = Query(7, ge=0, le=100),
) -> Dict[str, Any]:
    """Create a database backup."""
    from scripts.db_backup import run_backup
    return run_backup(output_dir=output_dir, compress=compress, rotate=rotate)


@app.get("/backup/list", tags=["backup"])
async def backup_list(
    output_dir: str = Query("backups"),
) -> Dict[str, Any]:
    """List available backups."""
    from scripts.db_backup import list_backups
    backups = list_backups(output_dir)
    return {"count": len(backups), "backups": backups}


@app.post("/backup/verify", tags=["backup"])
async def backup_verify(
    backup_path: str = Query(...),
) -> Dict[str, Any]:
    """Verify a backup file."""
    from scripts.db_backup import verify_backup
    return verify_backup(backup_path)


# ========== Deduplication ==========


@app.get("/dedup/status", tags=["deduplication"])
async def dedup_status() -> Dict[str, Any]:
    """Get deduplication store statistics."""
    from core.dedup import get_dedup_store
    return get_dedup_store().get_stats()


@app.post("/dedup/check", tags=["deduplication"])
async def dedup_check(
    message_id: Optional[str] = Query(None),
    sender: str = Query(""),
    subject: str = Query(""),
    body: str = Query(""),
) -> Dict[str, Any]:
    """Check if a conversation would be considered a duplicate."""
    from core.dedup import get_dedup_store
    store = get_dedup_store()
    is_dup = store.is_duplicate(message_id=message_id, sender=sender, subject=subject, body=body)
    return {
        "is_duplicate": is_dup,
        "checked_at": datetime.utcnow().isoformat(),
    }


@app.post("/dedup/cleanup", tags=["deduplication"])
async def dedup_cleanup() -> Dict[str, Any]:
    """Remove expired entries from the deduplication cache."""
    from core.dedup import get_dedup_store
    store = get_dedup_store()
    removed = store.cleanup_expired()
    return {
        "removed": removed,
        "stats": store.get_stats(),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/dedup/clear", tags=["deduplication"])
async def dedup_clear() -> Dict[str, Any]:
    """Clear all deduplication entries (use with caution)."""
    from core.dedup import get_dedup_store
    store = get_dedup_store()
    removed = store.clear()
    return {
        "removed": removed,
        "message": "Deduplication cache cleared",
        "timestamp": datetime.utcnow().isoformat(),
    }


# ========== Two-Factor Authentication ==========


@app.post("/auth/2fa/enable", tags=["auth"])
async def auth_2fa_enable(
    username: str = Form(...),
) -> Dict[str, Any]:
    """Enable 2FA for a user. Returns secret and QR URI."""
    from core.auth import enable_2fa
    result = enable_2fa(username)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/auth/2fa/verify-setup", tags=["auth"])
async def auth_2fa_verify_setup(
    username: str = Form(...),
    code: str = Form(...),
) -> Dict[str, Any]:
    """Verify initial TOTP code to activate 2FA."""
    from core.auth import verify_2fa_setup
    result = verify_2fa_setup(username, code)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/auth/2fa/verify", tags=["auth"])
async def auth_2fa_verify(
    username: str = Form(...),
    code: str = Form(...),
) -> Dict[str, Any]:
    """Verify a 2FA code (for login)."""
    from core.auth import verify_2fa
    is_valid = verify_2fa(username, code)
    return {
        "valid": is_valid,
        "username": username,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/auth/2fa/backup-code", tags=["auth"])
async def auth_2fa_backup_code(
    username: str = Form(...),
    code: str = Form(...),
) -> Dict[str, Any]:
    """Use a backup code for 2FA verification."""
    from core.auth import use_backup_code
    result = use_backup_code(username, code)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/auth/2fa/disable", tags=["auth"])
async def auth_2fa_disable(
    username: str = Form(...),
    password: str = Form(...),
) -> Dict[str, Any]:
    """Disable 2FA for a user (requires password confirmation)."""
    from core.auth import disable_2fa
    result = disable_2fa(username, password)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/auth/2fa/status/{username}", tags=["auth"])
async def auth_2fa_status(username: str) -> Dict[str, Any]:
    """Get 2FA status for a user."""
    from core.auth import get_2fa_status
    return get_2fa_status(username)


@app.get("/auth/2fa/enforcement", tags=["auth"])
async def auth_2fa_enforcement() -> Dict[str, Any]:
    """Get 2FA enforcement status for all users."""
    from core.auth import get_enforcement_status
    return get_enforcement_status()


@app.get("/auth/2fa/compliance/{username}", tags=["auth"])
async def auth_2fa_compliance(username: str) -> Dict[str, Any]:
    """Check if a user complies with 2FA enforcement."""
    from core.auth import check_2fa_compliance
    return check_2fa_compliance(username)


@app.get("/auth/2fa/non-compliant", tags=["auth"])
async def auth_2fa_non_compliant() -> Dict[str, Any]:
    """Get list of users who need to set up 2FA."""
    from core.auth import get_non_compliant_users
    users = get_non_compliant_users()
    return {
        "count": len(users),
        "users": users,
        "message": f"{len(users)} user(s) need to enable 2FA",
    }


# ========== 2FA Recovery Links ==========


@app.post("/auth/2fa/recovery/request", tags=["auth"])
async def auth_2fa_recovery_request(
    username: str = Query(...),
    send_email: bool = Query(True),
    base_url: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Generate and optionally send a 2FA recovery link."""
    from core.auth.recovery import generate_recovery_link, send_recovery_email

    result = generate_recovery_link(username, base_url=base_url)
    if "error" in result:
        return result

    if send_email:
        email_result = send_recovery_email(username, result["link"])
        result["email_sent"] = email_result.get("success", False)
        if not email_result.get("success"):
            result["manual_link"] = result["link"]
            result["message"] += " Note: Email not configured. Provide link manually."

    return result


@app.post("/auth/2fa/recovery/redeem", tags=["auth"])
async def auth_2fa_recovery_redeem(
    token: str = Query(...),
    disable_2fa: bool = Query(True),
) -> Dict[str, Any]:
    """Redeem a one-time recovery link to reset/disable 2FA."""
    from core.auth.recovery import redeem_recovery_link

    if disable_2fa:
        result = redeem_recovery_link(token)
    else:
        # Just verify the token is valid without redeeming
        from core.auth.recovery import _hash_token, _recovery_links
        token_hash = _hash_token(token)
        link_data = _recovery_links.get(token_hash)
        if link_data is None:
            return {"error": "Invalid token", "success": False}
        if time.time() > link_data["expires_at"]:
            return {"error": "Token expired", "success": False}
        if link_data["used"]:
            return {"error": "Token already used", "success": False}
        result = {"success": True, "valid": True, "username": link_data["username"]}

    return result


@app.get("/auth/2fa/recovery/pending", tags=["auth"])
async def auth_2fa_recovery_pending(
    username: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """List pending (unused) recovery links."""
    from core.auth.recovery import get_pending_recovery_links
    links = get_pending_recovery_links(username)
    return {
        "count": len(links),
        "links": links,
    }


@app.get("/auth/2fa/recovery/stats", tags=["auth"])
async def auth_2fa_recovery_stats() -> Dict[str, Any]:
    """Get recovery link statistics."""
    from core.auth.recovery import get_recovery_stats
    return get_recovery_stats()


@app.post("/auth/2fa/recovery/cleanup", tags=["auth"])
async def auth_2fa_recovery_cleanup() -> Dict[str, Any]:
    """Clean up expired recovery links."""
    from core.auth.recovery import cleanup_expired_links
    removed = cleanup_expired_links()
    return {"removed": removed, "message": f"Cleaned up {removed} expired link(s)"}


# ========== Export & Compliance ==========


@app.get("/export/conversations/csv", tags=["export"])
async def export_conversations_csv(
    source: Optional[str] = Query(None),
    urgency: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
) -> Response:
    """Export conversations to CSV for compliance reporting."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    exporter = get_export_manager()
    path = exporter.export_conversations_csv(
        source=source, urgency=urgency,
        since=since_dt, until=until_dt, limit=limit,
    )
    return FileResponse(
        path,
        media_type="text/csv",
        filename=os.path.basename(path),
    )


@app.get("/export/conversations/pdf", tags=["export"])
async def export_conversations_pdf(
    source: Optional[str] = Query(None),
    urgency: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> Response:
    """Export conversations to PDF for compliance reporting."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    exporter = get_export_manager()
    path = exporter.export_conversations_pdf(
        source=source, urgency=urgency,
        since=since_dt, until=until_dt, limit=limit,
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


@app.get("/export/drafts/csv", tags=["export"])
async def export_drafts_csv(
    sent_only: bool = Query(False),
    limit: int = Query(1000, ge=1, le=10000),
) -> Response:
    """Export follow-up drafts to CSV."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    exporter = get_export_manager()
    path = exporter.export_drafts_csv(sent_only=sent_only, limit=limit)
    return FileResponse(
        path,
        media_type="text/csv",
        filename=os.path.basename(path),
    )


@app.get("/export/audit/csv", tags=["export"])
async def export_audit_csv(
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    limit: int = Query(5000, ge=1, le=50000),
) -> Response:
    """Export audit trail to CSV."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    exporter = get_export_manager()
    path = exporter.export_audit_csv(
        action=action, entity_type=entity_type,
        since=since_dt, until=until_dt, limit=limit,
    )
    return FileResponse(
        path,
        media_type="text/csv",
        filename=os.path.basename(path),
    )


@app.get("/export/audit/pdf", tags=["export"])
async def export_audit_pdf(
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
) -> Response:
    """Export audit trail to PDF."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    exporter = get_export_manager()
    path = exporter.export_audit_pdf(
        action=action, entity_type=entity_type,
        since=since_dt, until=until_dt, limit=limit,
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


@app.get("/export/queue/csv", tags=["export"])
async def export_queue_csv() -> Response:
    """Export current queue state to CSV."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    exporter = get_export_manager()
    path = exporter.export_queue_csv()
    return FileResponse(
        path,
        media_type="text/csv",
        filename=os.path.basename(path),
    )


@app.get("/export/summary/pdf", tags=["export"])
async def export_summary_pdf() -> Response:
    """Export compliance summary report to PDF."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    exporter = get_export_manager()
    path = exporter.export_summary_pdf()
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


# Global exception handler — preserve HTTPException status codes
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail if isinstance(exc.detail, str) else str(exc.detail), "status_code": exc.status_code})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    # sanitize errors to be JSON serializable (ctx may contain ValueError)
    safe_errors = []
    for err in exc.errors():
        e = dict(err)
        if "ctx" in e and isinstance(e["ctx"], dict):
            e["ctx"] = {k: str(v) for k, v in e["ctx"].items()}
        safe_errors.append(e)
    return JSONResponse(status_code=422, content={"error": "validation_error", "details": safe_errors})

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled error at {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": "internal_server_error", "message": str(exc)})
