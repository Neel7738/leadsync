"""Tests for 2FA recovery email with one-time use links."""

import os
import time
import pytest

from core.auth import create_user, enable_2fa, verify_2fa_setup, get_2fa_status
from core.auth.recovery import (
    generate_recovery_link,
    redeem_recovery_link,
    send_recovery_email,
    get_pending_recovery_links,
    cleanup_expired_links,
    get_recovery_stats,
    _hash_token,
    _recovery_links,
)
from core.auth.totp import get_totp_manager


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path):
    """Provide isolated auth config."""
    import core.auth as auth_mod
    config_path = str(tmp_path / "auth.yaml")
    os.environ["AUTH_CONFIG_PATH"] = config_path
    auth_mod._authenticator = None
    # Clear recovery links
    _recovery_links.clear()
    yield config_path
    os.environ.pop("AUTH_CONFIG_PATH", None)
    auth_mod._authenticator = None
    _recovery_links.clear()


def _setup_2fa_user(username: str, config_path: str) -> str:
    """Create user with 2FA enabled and return the TOTP secret."""
    create_user(username, "password123", email=f"{username}@test.com", config_path=config_path)
    result = enable_2fa(username, config_path=config_path)
    secret = result["secret"]
    code = get_totp_manager().generate(secret)
    verify_2fa_setup(username, code, config_path=config_path)
    return secret


class TestGenerateRecoveryLink:
    def test_generates_link(self, isolated_auth):
        _setup_2fa_user("alice", isolated_auth)
        result = generate_recovery_link("alice", config_path=isolated_auth)
        assert "token" in result
        assert "link" in result
        assert result["username"] == "alice"
        assert result["expires_in_hours"] == 1

    def test_token_is_urlsafe(self, isolated_auth):
        _setup_2fa_user("bob", isolated_auth)
        result = generate_recovery_link("bob", config_path=isolated_auth)
        assert "token" in result
        # Token should be URL-safe base64
        assert all(c.isalnum() or c in "-_" for c in result["token"])

    def test_link_contains_token(self, isolated_auth):
        _setup_2fa_user("carol", isolated_auth)
        result = generate_recovery_link("carol", config_path=isolated_auth)
        assert result["token"] in result["link"]

    def test_custom_base_url(self, isolated_auth):
        _setup_2fa_user("dave", isolated_auth)
        result = generate_recovery_link(
            "dave",
            base_url="https://sfa.example.com",
            config_path=isolated_auth,
        )
        assert result["link"].startswith("https://sfa.example.com/auth/2fa/recovery/redeem")

    def test_custom_ttl(self, isolated_auth):
        _setup_2fa_user("eve", isolated_auth)
        result = generate_recovery_link("eve", ttl_hours=4, config_path=isolated_auth)
        assert result["expires_in_hours"] == 4
        expected_expiry = time.time() + (4 * 3600)
        assert abs(result["expires_at"] - expected_expiry) < 5

    def test_user_not_found(self, isolated_auth):
        result = generate_recovery_link("nobody", config_path=isolated_auth)
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_2fa_not_configured(self, isolated_auth):
        create_user("no2fa", "pass", config_path=isolated_auth)
        result = generate_recovery_link("no2fa", config_path=isolated_auth)
        assert "error" in result
        assert "2FA" in result["error"]

    def test_rate_limiting(self, isolated_auth):
        _setup_2fa_user("frank", isolated_auth)
        # Generate max allowed links
        for _ in range(3):
            result = generate_recovery_link("frank", config_path=isolated_auth)
            assert "error" not in result
        # Next one should be rate-limited
        result = generate_recovery_link("frank", config_path=isolated_auth)
        assert "error" in result
        assert "Too many" in result["error"]


class TestRedeemRecoveryLink:
    def test_redeem_disables_2fa(self, isolated_auth):
        _setup_2fa_user("grace", isolated_auth)
        gen = generate_recovery_link("grace", config_path=isolated_auth)
        result = redeem_recovery_link(gen["token"], config_path=isolated_auth)
        assert result["success"] is True
        assert result["action"] == "2fa_disabled"
        # 2FA should now be disabled
        status = get_2fa_status("grace", config_path=isolated_auth)
        assert status["enabled"] is False

    def test_redeem_sets_new_secret(self, isolated_auth):
        _setup_2fa_user("hank", isolated_auth)
        gen = generate_recovery_link("hank", config_path=isolated_auth)
        new_secret = get_totp_manager().generate_secret()
        result = redeem_recovery_link(
            gen["token"],
            new_totp_secret=new_secret,
            config_path=isolated_auth,
        )
        assert result["success"] is True
        assert result["action"] == "verify_2fa_setup"

    def test_one_time_use(self, isolated_auth):
        _setup_2fa_user("iris", isolated_auth)
        gen = generate_recovery_link("iris", config_path=isolated_auth)
        # First use succeeds
        result1 = redeem_recovery_link(gen["token"], config_path=isolated_auth)
        assert result1["success"] is True
        # Second use fails
        result2 = redeem_recovery_link(gen["token"], config_path=isolated_auth)
        assert result2["success"] is False
        assert "already used" in result2["error"]

    def test_expired_token(self, isolated_auth):
        _setup_2fa_user("jack", isolated_auth)
        gen = generate_recovery_link("jack", config_path=isolated_auth)
        # Manually expire the link
        token_hash = _hash_token(gen["token"])
        _recovery_links[token_hash]["expires_at"] = time.time() - 100
        result = redeem_recovery_link(gen["token"], config_path=isolated_auth)
        assert result["success"] is False
        assert "expired" in result["error"]

    def test_invalid_token(self, isolated_auth):
        result = redeem_recovery_link("invalid-token", config_path=isolated_auth)
        assert result["success"] is False
        assert "Invalid" in result["error"]


class TestSendRecoveryEmail:
    def test_no_smtp_configured(self, isolated_auth):
        _setup_2fa_user("kate", isolated_auth)
        gen = generate_recovery_link("kate", config_path=isolated_auth)
        result = send_recovery_email("kate", gen["link"], config_path=isolated_auth)
        # Without SMTP configured, should return error
        assert result["success"] is False or result.get("send_mode") == "manual"

    def test_user_not_found(self, isolated_auth):
        result = send_recovery_email("nobody", "http://example.com/recovery")
        assert result["success"] is False
        assert "not found" in result.get("error", "")


class TestPendingLinks:
    def test_list_pending(self, isolated_auth):
        _setup_2fa_user("leo", isolated_auth)
        gen1 = generate_recovery_link("leo", config_path=isolated_auth)
        gen2 = generate_recovery_link("leo", config_path=isolated_auth)
        pending = get_pending_recovery_links()
        assert len(pending) == 2
        assert all(p["username"] == "leo" for p in pending)

    def test_used_links_not_pending(self, isolated_auth):
        _setup_2fa_user("mia", isolated_auth)
        gen = generate_recovery_link("mia", config_path=isolated_auth)
        redeem_recovery_link(gen["token"], config_path=isolated_auth)
        pending = get_pending_recovery_links()
        assert len(pending) == 0

    def test_filter_by_username(self, isolated_auth):
        _setup_2fa_user("nick", isolated_auth)
        _setup_2fa_user("olivia", isolated_auth)
        generate_recovery_link("nick", config_path=isolated_auth)
        generate_recovery_link("olivia", config_path=isolated_auth)
        pending = get_pending_recovery_links(username="nick")
        assert len(pending) == 1
        assert pending[0]["username"] == "nick"


class TestCleanupExpired:
    def test_removes_expired(self, isolated_auth):
        _setup_2fa_user("pat", isolated_auth)
        gen = generate_recovery_link("pat", config_path=isolated_auth)
        # Manually expire
        token_hash = _hash_token(gen["token"])
        _recovery_links[token_hash]["expires_at"] = time.time() - 100
        removed = cleanup_expired_links()
        assert removed == 1
        assert token_hash not in _recovery_links

    def test_keeps_valid(self, isolated_auth):
        _setup_2fa_user("quinn", isolated_auth)
        generate_recovery_link("quinn", config_path=isolated_auth)
        removed = cleanup_expired_links()
        assert removed == 0
        assert len(_recovery_links) == 1


class TestRecoveryStats:
    def test_empty_stats(self, isolated_auth):
        stats = get_recovery_stats()
        assert stats["total_links"] == 0
        assert stats["pending"] == 0

    def test_stats_after_generation(self, isolated_auth):
        _setup_2fa_user("ruth", isolated_auth)
        generate_recovery_link("ruth", config_path=isolated_auth)
        generate_recovery_link("ruth", config_path=isolated_auth)
        stats = get_recovery_stats()
        assert stats["total_links"] == 2
        assert stats["pending"] == 2

    def test_stats_after_redeem(self, isolated_auth):
        _setup_2fa_user("sam", isolated_auth)
        gen1 = generate_recovery_link("sam", config_path=isolated_auth)
        gen2 = generate_recovery_link("sam", config_path=isolated_auth)
        redeem_recovery_link(gen1["token"], config_path=isolated_auth)
        stats = get_recovery_stats()
        assert stats["used"] == 1
        assert stats["pending"] == 1


class TestHashToken:
    def test_deterministic(self):
        token = "test-token-123"
        assert _hash_token(token) == _hash_token(token)

    def test_unique(self):
        import secrets
        t1 = secrets.token_urlsafe(32)
        t2 = secrets.token_urlsafe(32)
        assert _hash_token(t1) != _hash_token(t2)

    def test_sha256(self):
        import hashlib
        token = "abc"
        expected = hashlib.sha256(b"abc").hexdigest()
        assert _hash_token(token) == expected


class TestRecoveryLinkExpiry:
    def test_custom_expiry(self, isolated_auth):
        _setup_2fa_user("tina", isolated_auth)
        gen = generate_recovery_link("tina", ttl_hours=2, config_path=isolated_auth)
        token_hash = _hash_token(gen["token"])
        # Should not be expired yet (within 2 hours)
        assert _recovery_links[token_hash]["expires_at"] > time.time()
        # Manually set to expire in 1 second
        _recovery_links[token_hash]["expires_at"] = time.time() + 1
        time.sleep(1.5)
        # Now should be expired
        result = redeem_recovery_link(gen["token"], config_path=isolated_auth)
        assert result["success"] is False
