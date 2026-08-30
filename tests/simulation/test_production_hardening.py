"""
Production hardening verification — ensures fixes are in place.
"""
from datetime import datetime, timedelta
from core.models.conversation import Conversation
from core.ingest.meeting import process_meeting_notes
from core.ingest.email import _heuristic_urgency_and_deal
from core.intelligence.scorer import score_prospect

def test_meeting_date_parsing_fixed():
    c = process_meeting_notes("Meeting on 03/15/2024 with John Doe: Action Item: send proposal\nAttendees: Alice Smith, Bob Jones", source="meeting")
    assert c.date.year == 2024
    assert c.date.month == 3
    assert c.date.day == 15

def test_meeting_date_iso():
    c = process_meeting_notes("Sprint 2024-12-01 notes: Follow-up needed", source="meeting")
    assert c.date.year == 2024

def test_heuristic_urgency_and_deal():
    urgency, deal = _heuristic_urgency_and_deal("Need this ASAP urgent $50,000 deal")
    assert urgency == "high"
    assert deal == 50000

def test_scorer_fractional_recency():
    conv = Conversation(source="email", participants=[{"name":"A","email":"a@b.com"}], raw_text="hello", date=datetime.utcnow() - timedelta(hours=12))
    scored = score_prospect(conv)
    assert abs(scored.recency_days - 0.5) < 0.05

def test_bcrypt_hash_roundtrip():
    from core.auth import hash_password, verify_password
    ph = hash_password("test12345")
    assert ph.startswith("$2b$") or ":" in ph
    assert verify_password("test12345", ph) is True
    assert verify_password("wrong", ph) is False

def test_api_key_collision_free():
    from core.auth import APIKeyManager
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        path = f.name
    try:
        m = APIKeyManager(config_path=path)
        k1 = m.create_key("ci1")
        k2 = m.create_key("ci2")
        assert k1 != k2
        assert m.verify_key(k1) is not None
        assert m.verify_key(k2) is not None
        assert m.verify_key(k1)["name"] == "ci1"
        assert m.revoke_key(k1) is True
        assert m.verify_key(k1) is None
    finally:
        try: os.unlink(path)
        except: pass
        try: os.unlink(path.replace(".yaml",".json"))
        except: pass

def test_tel_realtime_broadcast_no_crash():
    from core.realtime import emit_queue_event
    # should not raise even with no running loop
    emit_queue_event("queue:test", conversation_id="x")

def test_queue_max_eviction():
    from core.queue import PriorityQueue
    q = PriorityQueue()
    for i in range(55):
        conv = Conversation(source="email", participants=[{"name":"A","email":"a@b.com"}], raw_text=f"msg {i}", urgency="low", deal_size=1000)
        scored = score_prospect(conv, deal_value=1000)
        q.add(scored)
    assert q.size() <= 50

def test_exception_handler_preserves_422():
    from fastapi.testclient import TestClient
    from api.app import app
    c = TestClient(app)
    r = c.post("/score", json={"source":"bad","participants":[],"raw_text":"hi"})
    assert r.status_code == 422
    assert r.json().get("error") == "validation_error"
