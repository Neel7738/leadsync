"""Tests for configuration."""

import os
import pytest
from core.config import get_settings, reload_settings


class TestSettings:
    def test_singleton(self):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_reload(self):
        s1 = get_settings()
        s2 = reload_settings()
        assert s1 is not s2  # New instance after reload

    def test_defaults(self):
        s = get_settings()
        assert s.llm_provider in ("ollama", "openai", "anthropic", "nim", "google", "groq")
        assert s.sla_high_urgency_hours == 24
        assert s.sla_medium_urgency_hours == 48
        assert s.sla_low_urgency_hours == 72
        assert s.port == 8000

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        reload_settings()
        s = get_settings()
        assert s.llm_provider == "anthropic"
        # Reset
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        reload_settings()

    def test_get_all_config(self):
        s = get_settings()
        if hasattr(s, "get_all_config"):
            config = s.get_all_config()
            assert isinstance(config, dict)
            assert "llm_provider" in config

    def test_ollama_settings(self):
        s = get_settings()
        assert hasattr(s, "ollama_host")
        assert hasattr(s, "ollama_model_primary")
        assert hasattr(s, "ollama_model_fallback")

    def test_smtp_settings(self):
        s = get_settings()
        assert hasattr(s, "smtp_host")
        assert hasattr(s, "smtp_port")
        assert hasattr(s, "smtp_username")
