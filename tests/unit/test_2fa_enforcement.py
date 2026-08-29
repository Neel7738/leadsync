"""Tests for 2FA enforcement policy."""

import os
import pytest

from core.auth import (
    create_user, enable_2fa, verify_2fa_setup,
    is_2fa_required, check_2fa_compliance,
    get_enforcement_status, get_non_compliant_users,
    get_2fa_status, delete_user,
)
from core.auth.totp import get_totp_manager


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path):
    """Provide isolated auth config (no default admin)."""
    import core.auth as auth_mod
    config_path = str(tmp_path / "auth.yaml")
    os.environ["AUTH_CONFIG_PATH"] = config_path
    auth_mod._authenticator = None
    # Remove default admin created by _load_config
    delete_user("admin", config_path=config_path)
    yield config_path
    os.environ.pop("AUTH_CONFIG_PATH", None)
    os.environ.pop("ENFORCE_2FA_ADMIN", None)
    os.environ.pop("ENFORCE_2FA_REP", None)
    auth_mod._authenticator = None
    # Reset cached settings so env var changes take effect
    from core.config import reload_settings
    reload_settings()


class TestIs2FARequired:
    def test_admin_not_required_by_default(self, isolated_auth):
        create_user("admin1", "pass", role="admin")
        assert is_2fa_required("admin1") is False

    def test_admin_required_when_enforced(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("admin2", "pass", role="admin")
        assert is_2fa_required("admin2") is True

    def test_rep_not_required_by_default(self, isolated_auth):
        create_user("rep1", "pass", role="rep")
        assert is_2fa_required("rep1") is False

    def test_rep_required_when_enforced(self, isolated_auth):
        os.environ["ENFORCE_2FA_REP"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("rep2", "pass", role="rep")
        assert is_2fa_required("rep2") is True

    def test_admin_inherits_rep_enforcement(self, isolated_auth):
        os.environ["ENFORCE_2FA_REP"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("admin3", "pass", role="admin")
        # Admin is also "rep or above" so enforce_rep applies
        assert is_2fa_required("admin3") is True

    def test_viewer_not_affected(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        os.environ["ENFORCE_2FA_REP"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("viewer1", "pass", role="viewer")
        assert is_2fa_required("viewer1") is False

    def test_nonexistent_user(self):
        assert is_2fa_required("nobody") is False


class TestCheck2FACompliance:
    def test_compliant_when_not_required(self, isolated_auth):
        create_user("user1", "pass", role="rep")
        result = check_2fa_compliance("user1")
        assert result["compliant"] is True
        assert result["required"] is False

    def test_compliant_when_2fa_enabled(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("admin_ok", "pass", role="admin")
        result_setup = enable_2fa("admin_ok")
        code = get_totp_manager().generate(result_setup["secret"])
        verify_2fa_setup("admin_ok", code)

        result = check_2fa_compliance("admin_ok")
        assert result["compliant"] is True
        assert result["required"] is True

    def test_non_compliant_when_2fa_required_but_disabled(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("admin_bad", "pass", role="admin")
        result = check_2fa_compliance("admin_bad")
        assert result["compliant"] is False
        assert result["required"] is True
        assert result["action_required"] == "enable_2fa"

    def test_nonexistent_user(self):
        result = check_2fa_compliance("nobody")
        assert result["compliant"] is False
        assert "error" in result


class TestEnforcementStatus:
    def test_all_compliant(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("a1", "pass", role="admin")
        result_setup = enable_2fa("a1")
        code = get_totp_manager().generate(result_setup["secret"])
        verify_2fa_setup("a1", code)

        status = get_enforcement_status()
        assert status["enforce_admin"] is True
        assert status["compliant_users"] == 1
        assert status["non_compliant_users"] == []
        assert status["compliance_rate"] == 100.0

    def test_partial_compliance(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        from core.config import reload_settings
        reload_settings()

        # One compliant admin
        create_user("good_admin", "pass", role="admin")
        result_setup = enable_2fa("good_admin")
        code = get_totp_manager().generate(result_setup["secret"])
        verify_2fa_setup("good_admin", code)

        # One non-compliant admin
        create_user("bad_admin", "pass", role="admin")

        status = get_enforcement_status()
        assert status["compliant_users"] == 1
        assert len(status["non_compliant_users"]) == 1
        assert "bad_admin" in status["non_compliant_users"]
        assert 0 < status["compliance_rate"] < 100

    def test_no_enforcement(self, isolated_auth):
        create_user("free_user", "pass", role="admin")
        status = get_enforcement_status()
        assert status["enforce_admin"] is False
        assert status["compliance_rate"] == 100.0
        assert status["non_compliant_users"] == []


class TestGetNonCompliantUsers:
    def test_returns_empty_when_all_compliant(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("ok_admin", "pass", role="admin")
        result_setup = enable_2fa("ok_admin")
        code = get_totp_manager().generate(result_setup["secret"])
        verify_2fa_setup("ok_admin", code)

        users = get_non_compliant_users()
        assert len(users) == 0

    def test_returns_non_compliant(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("bad_admin", "pass", role="admin")
        users = get_non_compliant_users()
        assert len(users) == 1
        assert users[0]["username"] == "bad_admin"
        assert users[0]["action_required"] == "enable_2fa"


class TestStreamlitIntegration:
    """Test that the dashboard can check enforcement status."""

    def test_enforcement_status_for_dashboard(self, isolated_auth):
        os.environ["ENFORCE_2FA_ADMIN"] = "true"
        from core.config import reload_settings
        reload_settings()

        create_user("dash_admin", "pass", role="admin")
        create_user("dash_rep", "pass", role="rep")

        status = get_enforcement_status()
        # Dashboard should be able to render this
        assert "compliance_rate" in status
        assert "non_compliant_users" in status
        assert isinstance(status["details"], list)
        assert len(status["details"]) == 2
