"""Tests for the auth module."""

import json
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path):
    """Provide an isolated auth config for each test."""
    import core.auth as auth_mod

    config_path = str(tmp_path / "auth_config.yaml")
    os.environ["AUTH_CONFIG_PATH"] = config_path
    # Reset global singletons so each test starts fresh
    auth_mod._authenticator = None
    auth_mod._api_key_manager = None
    yield config_path
    os.environ.pop("AUTH_CONFIG_PATH", None)
    auth_mod._authenticator = None
    auth_mod._api_key_manager = None


# ── Password Hashing ───────────────────────────────────────────

class TestPasswordHashing:
    def test_hash_returns_salt_and_hash(self):
        from core.auth import hash_password
        result = hash_password("testpass")
        assert ":" in result
        salt, hashed = result.split(":", 1)
        assert len(salt) == 32  # hex(16 bytes)
        assert len(hashed) == 64  # sha256 hex

    def test_verify_correct_password(self):
        from core.auth import hash_password, verify_password
        stored = hash_password("mypassword")
        assert verify_password("mypassword", stored) is True

    def test_verify_wrong_password(self):
        from core.auth import hash_password, verify_password
        stored = hash_password("mypassword")
        assert verify_password("wrongpassword", stored) is False

    def test_same_password_different_hashes(self):
        from core.auth import hash_password
        h1 = hash_password("samepass")
        h2 = hash_password("samepass")
        assert h1 != h2  # Different salts

    def test_verify_invalid_format(self):
        from core.auth import verify_password
        assert verify_password("pass", "no-colon") is False
        assert verify_password("pass", None) is False


# ── User Management ────────────────────────────────────────────

class TestUserManagement:
    def test_create_user(self, isolated_auth):
        from core.auth import create_user, get_user
        result = create_user("alice", "pass123", name="Alice", email="alice@test.com", role="rep", team="enterprise")
        assert result is True
        user = get_user("alice")
        assert user is not None
        assert user["name"] == "Alice"
        assert user["role"] == "rep"
        assert user["team"] == "enterprise"

    def test_create_duplicate_user(self, isolated_auth):
        from core.auth import create_user
        create_user("bob", "pass1")
        result = create_user("bob", "pass2")
        assert result is False

    def test_list_users(self, isolated_auth):
        from core.auth import create_user, list_users
        create_user("u1", "p1", name="User 1", role="rep")
        create_user("u2", "p2", name="User 2", role="admin")
        users = list_users()
        # May include default "admin" user auto-created by config init
        created_names = {u["username"] for u in users if u["username"] in ("u1", "u2")}
        assert created_names == {"u1", "u2"}
        # Passwords not included
        for u in users:
            assert "password" not in u

    def test_delete_user(self, isolated_auth):
        from core.auth import create_user, delete_user, get_user
        create_user("delme", "pass")
        assert delete_user("delme") is True
        assert get_user("delme") is None

    def test_delete_nonexistent_user(self, isolated_auth):
        from core.auth import delete_user
        assert delete_user("ghost") is False

    def test_update_password(self, isolated_auth):
        from core.auth import create_user, update_password, verify_password, get_user
        create_user("updateme", "oldpass")
        assert update_password("updateme", "newpass") is True
        user = get_user("updateme")
        assert verify_password("newpass", user["password"]) is True
        assert verify_password("oldpass", user["password"]) is False

    def test_get_nonexistent_user(self, isolated_auth):
        from core.auth import get_user
        assert get_user("nobody") is None


# ── Authenticator ──────────────────────────────────────────────

class TestAuthenticator:
    def test_successful_login(self, isolated_auth):
        from core.auth import create_user, get_authenticator
        create_user("loginuser", "secret123", name="Login User", role="rep")
        auth = get_authenticator()
        success, msg, user_data = auth.authenticate("loginuser", "secret123")
        assert success is True
        assert "successful" in msg.lower()
        assert user_data is not None
        assert user_data["username"] == "loginuser"
        assert user_data["role"] == "rep"

    def test_wrong_password(self, isolated_auth):
        from core.auth import create_user, get_authenticator
        create_user("wpuser", "correct")
        auth = get_authenticator()
        success, msg, user_data = auth.authenticate("wpuser", "wrong")
        assert success is False
        assert user_data is None

    def test_nonexistent_user(self, isolated_auth):
        from core.auth import get_authenticator
        auth = get_authenticator()
        success, msg, user_data = auth.authenticate("ghost", "pass")
        assert success is False
        assert user_data is None

    def test_session_verification(self, isolated_auth):
        from core.auth import create_user, get_authenticator
        create_user("sessuser", "pass")
        auth = get_authenticator()
        success, _, user_data = auth.authenticate("sessuser", "pass")
        assert success is True

        # Verify session via a token (extract from internal state)
        token = list(auth._sessions.keys())[0]
        verified = auth.verify_session(token)
        assert verified is not None
        assert verified["username"] == "sessuser"

    def test_invalid_session_token(self, isolated_auth):
        from core.auth import get_authenticator
        auth = get_authenticator()
        assert auth.verify_session("bogus_token") is None

    def test_logout(self, isolated_auth):
        from core.auth import create_user, get_authenticator
        create_user("logoutuser", "pass")
        auth = get_authenticator()
        auth.authenticate("logoutuser", "pass")
        token = list(auth._sessions.keys())[0]
        auth.logout(token)
        assert auth.verify_session(token) is None

    def test_brute_force_lockout(self, isolated_auth):
        from core.auth import create_user, get_authenticator
        create_user("lockme", "correct")
        auth = get_authenticator()

        # Fail 5 times
        for _ in range(5):
            auth.authenticate("lockme", "wrong")

        # 6th attempt should be locked out even with correct password
        success, msg, _ = auth.authenticate("lockme", "correct")
        assert success is False
        assert "locked" in msg.lower()

    def test_session_count(self, isolated_auth):
        from core.auth import create_user, get_authenticator
        create_user("scuser", "pass")
        auth = get_authenticator()
        assert auth.get_session_count() == 0
        auth.authenticate("scuser", "pass")
        assert auth.get_session_count() == 1


# ── API Key Management ─────────────────────────────────────────

class TestAPIKeys:
    def test_create_and_verify_key(self, isolated_auth):
        from core.auth import get_api_key_manager
        mgr = get_api_key_manager()
        key = mgr.create_key("Test Key", role="rep")
        assert key.startswith("sfa_")
        metadata = mgr.verify_key(key)
        assert metadata is not None
        assert metadata["name"] == "Test Key"
        assert metadata["role"] == "rep"

    def test_verify_invalid_key(self, isolated_auth):
        from core.auth import get_api_key_manager
        mgr = get_api_key_manager()
        assert mgr.verify_key("sfa_invalid_key_here") is None

    def test_revoke_key(self, isolated_auth):
        from core.auth import get_api_key_manager
        mgr = get_api_key_manager()
        key = mgr.create_key("Revoke Me")
        assert mgr.revoke_key(key) is True
        assert mgr.verify_key(key) is None

    def test_revoke_nonexistent_key(self, isolated_auth):
        from core.auth import get_api_key_manager
        mgr = get_api_key_manager()
        assert mgr.revoke_key("sfa_no_such_key") is False


# ── Role Checking ──────────────────────────────────────────────

class TestRoles:
    def test_require_role_admin(self):
        from core.auth import require_role
        assert require_role({"role": "admin"}, "admin") is True
        assert require_role({"role": "rep"}, "admin") is False

    def test_require_role_multiple(self):
        from core.auth import require_role
        assert require_role({"role": "rep"}, "admin", "rep") is True
        assert require_role({"role": "viewer"}, "admin", "rep") is False

    def test_is_admin(self):
        from core.auth import is_admin
        assert is_admin({"role": "admin"}) is True
        assert is_admin({"role": "rep"}) is False
        assert is_admin(None) is False

    def test_is_rep_or_above(self):
        from core.auth import is_rep_or_above
        assert is_rep_or_above({"role": "admin"}) is True
        assert is_rep_or_above({"role": "rep"}) is True
        assert is_rep_or_above({"role": "viewer"}) is False
