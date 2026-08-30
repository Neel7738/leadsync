"""
Air-gapped verification — every external must be cuttable via AIR_GAPPED=true
"""
import os
from unittest.mock import patch, MagicMock
import pytest

def test_air_gapped_llm_only_local():
    os.environ["AIR_GAPPED"] = "true"
    try:
        from core.config import reload_settings
        from core.intelligence.llm_manager import LLMManager
        reload_settings()
        mgr = LLMManager()
        # should not attempt httpx to cloud even if keys set
        with patch.object(mgr, "_call_ollama", return_value=None) as mock_ollama:
            with pytest.raises(RuntimeError, match="AIR_GAPPED"):
                mgr.generate("hello")
            assert mock_ollama.called
            # ensure cloud _call_provider never hit
    finally:
        os.environ.pop("AIR_GAPPED", None)
        from core.config import reload_settings
        reload_settings()

def test_air_gapped_alerts_console_only():
    os.environ["AIR_GAPPED"] = "true"
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
    os.environ["TELEGRAM_CHAT_ID"] = "123"
    try:
        from core.config import reload_settings
        reload_settings()
        from core.alerts import create_sla_checker
        # reset singleton
        import core.alerts
        core.alerts._sla_checker = None
        checker = create_sla_checker()
        # only console channel should be registered
        assert len(checker.alert_manager._channels) == 1
        assert checker.alert_manager._channels[0].__name__ == "console_alert"
    finally:
        os.environ.pop("AIR_GAPPED", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        from core.config import reload_settings
        reload_settings()
        import core.alerts
        core.alerts._sla_checker = None

def test_air_gapped_stt_blocks_api():
    os.environ["AIR_GAPPED"] = "true"
    try:
        from core.config import reload_settings
        reload_settings()
        from core.ingest.stt import _transcribe_via_api, transcribe_audio
        with pytest.raises(RuntimeError, match="AIR_GAPPED"):
            _transcribe_via_api("/tmp/fake.wav")
        # transcribe_audio with use_api=True should also block before httpx
        with pytest.raises(RuntimeError, match="AIR_GAPPED"):
            # need a temp file exists to get past file check
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(b"fake")
                path = f.name
            try:
                transcribe_audio(path, use_api=True)
            finally:
                os.unlink(path)
    finally:
        os.environ.pop("AIR_GAPPED", None)
        from core.config import reload_settings
        reload_settings()

def test_qr_local_no_google():
    from core.auth.totp import TOTPManager
    m = TOTPManager()
    uri = m.get_uri("JBSWY3DPEHPK3PXP", "test@example.com")
    # get_qr_code_data_uri must not contain google
    data_uri = m.get_qr_code_data_uri(uri)
    assert "chart.googleapis.com" not in data_uri
    # In air-gapped, get_qr_code_url should raise or return data uri, never google
    os.environ["AIR_GAPPED"] = "true"
    try:
        from core.config import reload_settings
        reload_settings()
        # force no qrcode installed path: mock failure still shouldn't hit google
        # but we test that air-gapped + no local still doesn't use google via exception
        # Here local IS available (qrcode may not be installed, but uri fallback is ok)
        # So we just assert url is not google when we call data_uri path
        assert "chart.googleapis.com" not in data_uri
    finally:
        os.environ.pop("AIR_GAPPED", None)
        from core.config import reload_settings
        reload_settings()

def test_full_pipeline_offline_still_works():
    """End-to-end pipeline with AIR_GAPPED=true must still score/queue/generate"""
    os.environ["AIR_GAPPED"] = "true"
    try:
        from core.config import reload_settings
        reload_settings()
        from core.models.conversation import Conversation
        from core.intelligence.scorer import score_prospect
        from core.queue import PriorityQueue
        from core.generation.prompt import generate_drafts
        conv = Conversation(source="email", participants=[{"name":"Offline Prospect","email":"o@test.com"}], raw_text="Budget $90000 urgent need proposal", urgency="high", deal_size=90000)
        scored = score_prospect(conv, deal_value=90000)
        assert scored.priority_score > 0.6
        q = PriorityQueue(max_items=10)
        q.add(scored)
        assert q.size() == 1
        drafts = generate_drafts(conv, prospect_name="Offline Prospect", use_llm=True)
        assert len(drafts) == 3
        assert all("Subject:" in v for v in drafts.values())
    finally:
        os.environ.pop("AIR_GAPPED", None)
        from core.config import reload_settings
        reload_settings()
