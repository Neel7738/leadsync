"""Tests for TOTP and two-factor authentication."""

import os
import time
from unittest.mock import patch

import pytest

from core.auth.totp import TOTPManager, BackupCodes, get_totp_manager, get_backup_codes


@pytest.fixture(autouse=True)
def clean_auth():
    """Reset global instances before each test."""
    import core.auth as auth_mod
    import core.auth.totp as totp_mod
    auth_mod._authenticator = None
    totp_mod._totp_manager = None
    totp_mod._backup_codes = None
    yield
    auth_mod._authenticator = None
    totp_mod._totp_manager = None
    totp_mod._backup_codes = None


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path):
    """Provide isolated auth config."""
    import core.auth as auth_mod
    config_path = str(tmp_path / "auth.yaml")
    os.environ["AUTH_CONFIG_PATH"] = config_path
    auth_mod._authenticator = None
    yield config_path
    os.environ.pop("AUTH_CONFIG_PATH", None)
    auth_mod._authenticator = None


# ── TOTP Generation ────────────────────────────────────────────


class TestTOTPManager:
    def test_generate_secret(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        assert len(secret) > 0
        # Base32 chars only
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret)

    def test_generate_secret_unique(self):
        totp = TOTPManager()
        s1 = totp.generate_secret()
        s2 = totp.generate_secret()
        assert s1 != s2

    def test_generate_otp_returns_string(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        otp = totp.generate(secret)
        assert isinstance(otp, str)
        assert len(otp) == 6
        assert otp.isdigit()

    def test_generate_otp_consistent_for_same_time(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        # Same counter = same OTP
        otp1 = totp._generate_otp(secret, 1000)
        otp2 = totp._generate_otp(secret, 1000)
        assert otp1 == otp2

    def test_generate_otp_different_for_different_time(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        otp1 = totp._generate_otp(secret, 1000)
        otp2 = totp._generate_otp(secret, 1001)
        assert otp1 != otp2

    def test_verify_valid_code(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        code = totp.generate(secret)
        assert totp.verify(secret, code) is True

    def test_verify_invalid_code(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        assert totp.verify(secret, "000000") is False

    def test_verify_empty_code(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        assert totp.verify(secret, "") is False
        assert totp.verify(secret, None) is False

    def test_verify_with_window(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        # Generate code for previous time step
        code = totp.generate(secret, time_offset=-1)
        # Should verify with window=1
        assert totp.verify(secret, code, window=1) is True
        # Should fail without window
        assert totp.verify(secret, code, window=0) is False

    def test_get_uri_format(self):
        totp = TOTPManager()
        secret = totp.generate_secret()
        uri = totp.get_uri(secret, "user@company.com", "TestApp")
        assert uri.startswith("otpauth://totp/")
        assert secret in uri
        assert "TestApp" in uri
        assert "user%40company.com" in uri or "user@company.com" in uri

    def test_different_digits(self):
        totp = TOTPManager(digits=8)
        secret = totp.generate_secret()
        otp = totp.generate(secret)
        assert len(otp) == 8

    def test_different_interval(self):
        totp = TOTPManager(interval=60)
        secret = totp.generate_secret()
        otp = totp.generate(secret)
        assert len(otp) == 6


# ── Backup Codes ───────────────────────────────────────────────


class TestBackupCodes:
    def test_generate_returns_list(self):
        bc = BackupCodes()
        codes = bc.generate()
        assert isinstance(codes, list)
        assert len(codes) == 10

    def test_generate_unique_codes(self):
        bc = BackupCodes()
        codes = bc.generate()
        assert len(set(codes)) == len(codes)

    def test_code_format(self):
        bc = BackupCodes()
        codes = bc.generate()
        for code in codes:
            assert "-" in code
            parts = code.split("-")
            assert len(parts) == 2

    def test_hash_and_verify(self):
        bc = BackupCodes()
        codes = bc.generate()
        hashed = bc.hash_codes(codes)

        # First code should verify
        is_valid, index = bc.verify_code(codes[0], hashed)
        assert is_valid is True
        assert index == 0

    def test_invalid_code_fails(self):
        bc = BackupCodes()
        codes = bc.generate()
        hashed = bc.hash_codes(codes)

        is_valid, index = bc.verify_code("XXXX-XXXX", hashed)
        assert is_valid is False
        assert index is None

    def test_remove_used_code(self):
        bc = BackupCodes()
        codes = bc.generate()
        hashed = bc.hash_codes(codes)

        _, index = bc.verify_code(codes[3], hashed)
        remaining = bc.remove_used(hashed, index)
        assert len(remaining) == len(hashed) - 1

    def test_code_case_insensitive(self):
        bc = BackupCodes()
        codes = bc.generate()
        hashed = bc.hash_codes(codes)

        # Uppercase version should also work
        is_valid, _ = bc.verify_code(codes[0].upper(), hashed)
        assert is_valid is True

    def test_code_without_dash(self):
        bc = BackupCodes()
        codes = bc.generate()
        hashed = bc.hash_codes(codes)

        # Code without dash should also work
        code_no_dash = codes[0].replace("-", "")
        is_valid, _ = bc.verify_code(code_no_dash, hashed)
        assert is_valid is True


# ── 2FA Integration ────────────────────────────────────────────


class Test2FAIntegration:
    def test_enable_2fa(self, isolated_auth):
        from core.auth import create_user, enable_2fa, get_user
        create_user("alice", "pass123", name="Alice", email="alice@co.com", role="admin")

        result = enable_2fa("alice")
        assert "secret" in result
        assert "uri" in result
        assert "backup_codes" in result
        assert len(result["backup_codes"]) == 10

        # User should have TOTP fields
        user = get_user("alice")
        assert user.get("totp_secret") == result["secret"]
        assert user.get("totp_enabled") is False  # Not yet active

    def test_verify_2fa_setup(self, isolated_auth):
        from core.auth import create_user, enable_2fa, verify_2fa_setup
        create_user("bob", "pass123", email="bob@co.com")

        result = enable_2fa("bob")
        secret = result["secret"]

        # Generate a valid code
        totp = get_totp_manager()
        code = totp.generate(secret)

        # Verify setup
        verify_result = verify_2fa_setup("bob", code)
        assert verify_result.get("success") is True

    def test_verify_2fa_setup_wrong_code(self, isolated_auth):
        from core.auth import create_user, enable_2fa, verify_2fa_setup
        create_user("carol", "pass123")

        enable_2fa("carol")
        result = verify_2fa_setup("carol", "000000")
        assert result.get("success") is False

    def test_verify_2fa_login(self, isolated_auth):
        from core.auth import create_user, enable_2fa, verify_2fa_setup, verify_2fa
        create_user("dave", "pass123")

        result = enable_2fa("dave")
        code = get_totp_manager().generate(result["secret"])
        verify_2fa_setup("dave", code)

        # Login with valid code
        login_code = get_totp_manager().generate(result["secret"])
        assert verify_2fa("dave", login_code) is True

    def test_verify_2fa_login_invalid(self, isolated_auth):
        from core.auth import create_user, enable_2fa, verify_2fa_setup, verify_2fa
        create_user("eve", "pass123")

        result = enable_2fa("eve")
        code = get_totp_manager().generate(result["secret"])
        verify_2fa_setup("eve", code)

        assert verify_2fa("eve", "000000") is False

    def test_2fa_not_enabled_passes(self, isolated_auth):
        from core.auth import create_user, verify_2fa
        create_user("frank", "pass123")

        # No 2FA — should auto-pass
        assert verify_2fa("frank", "anything") is True

    def test_backup_code_flow(self, isolated_auth):
        from core.auth import create_user, enable_2fa, verify_2fa_setup, use_backup_code
        create_user("grace", "pass123")

        result = enable_2fa("grace")
        backup_codes = result["backup_codes"]
        code = get_totp_manager().generate(result["secret"])
        verify_2fa_setup("grace", code)

        # Use a backup code
        use_result = use_backup_code("grace", backup_codes[0])
        assert use_result.get("success") is True
        assert use_result["remaining"] == 9

        # Same backup code shouldn't work again
        use_result2 = use_backup_code("grace", backup_codes[0])
        assert use_result2.get("success") is False

    def test_disable_2fa(self, isolated_auth):
        from core.auth import create_user, enable_2fa, verify_2fa_setup, disable_2fa, verify_2fa
        create_user("hal", "pass123")

        result = enable_2fa("hal")
        code = get_totp_manager().generate(result["secret"])
        verify_2fa_setup("hal", code)

        # Disable
        disable_result = disable_2fa("hal", "pass123")
        assert disable_result.get("success") is True

        # 2FA should now auto-pass
        assert verify_2fa("hal", "anything") is True

    def test_disable_2fa_wrong_password(self, isolated_auth):
        from core.auth import create_user, enable_2fa, verify_2fa_setup, disable_2fa
        create_user("ivy", "pass123")

        result = enable_2fa("ivy")
        code = get_totp_manager().generate(result["secret"])
        verify_2fa_setup("ivy", code)

        disable_result = disable_2fa("ivy", "wrongpass")
        assert "error" in disable_result

    def test_2fa_status(self, isolated_auth):
        from core.auth import create_user, enable_2fa, verify_2fa_setup, get_2fa_status
        create_user("jack", "pass123")

        # Before 2FA
        status = get_2fa_status("jack")
        assert status["enabled"] is False
        assert status["configured"] is False

        # After setup
        result = enable_2fa("jack")
        status = get_2fa_status("jack")
        assert status["enabled"] is False
        assert status["configured"] is True
        assert status["backup_codes_remaining"] == 10

        # After activation
        code = get_totp_manager().generate(result["secret"])
        verify_2fa_setup("jack", code)
        status = get_2fa_status("jack")
        assert status["enabled"] is True

    def test_enable_2fa_already_enabled(self, isolated_auth):
        from core.auth import create_user, enable_2fa
        create_user("kate", "pass123")
        enable_2fa("kate")

        result = enable_2fa("kate")
        assert "error" in result

    def test_list_users_includes_2fa(self, isolated_auth):
        from core.auth import create_user, enable_2fa, list_users
        create_user("leo", "pass123")
        enable_2fa("leo")

        users = list_users()
        leo = next(u for u in users if u["username"] == "leo")
        assert leo["totp_enabled"] is False
        assert leo["backup_codes_remaining"] == 10
