"""Tests for Alembic database migration operations."""

import os
import subprocess
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_alembic(*args, env_override=None):
    """Run alembic command and return result."""
    cmd = [sys.executable, "-m", "alembic"] + list(args)
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    if env_override:
        env.update(env_override)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT, env=env)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Use a temporary SQLite database for each test."""
    db_path = str(tmp_path / "test.db")
    url = f"sqlite:///{db_path}"
    os.environ["DATABASE_URL"] = url
    yield url
    os.environ.pop("DATABASE_URL", None)


# ── Alembic Config ─────────────────────────────────────────────


class TestAlembicConfig:
    def test_alembic_ini_exists(self):
        path = os.path.join(PROJECT_ROOT, "alembic.ini")
        assert os.path.exists(path), "alembic.ini must exist"

    def test_env_py_exists(self):
        path = os.path.join(PROJECT_ROOT, "alembic", "env.py")
        assert os.path.exists(path), "alembic/env.py must exist"

    def test_script_template_exists(self):
        path = os.path.join(PROJECT_ROOT, "alembic", "script.py.mako")
        assert os.path.exists(path), "alembic/script.py.mako must exist"

    def test_versions_dir_exists(self):
        path = os.path.join(PROJECT_ROOT, "alembic", "versions")
        assert os.path.isdir(path), "alembic/versions/ must exist"

    def test_initial_migration_exists(self):
        versions_dir = os.path.join(PROJECT_ROOT, "alembic", "versions")
        migrations = [f for f in os.listdir(versions_dir) if f.endswith(".py") and not f.startswith("__")]
        assert len(migrations) >= 1, "At least one migration file must exist"


# ── Migration Operations ───────────────────────────────────────


class TestMigrationUpgrade:
    def test_upgrade_head(self):
        """Apply all migrations to head."""
        result = run_alembic("upgrade", "head")
        assert result.returncode == 0, f"upgrade head failed: {result.stderr}"
        # Should not error

    def test_upgrade_idempotent(self):
        """Running upgrade twice should be safe."""
        run_alembic("upgrade", "head")
        result = run_alembic("upgrade", "head")
        assert result.returncode == 0

    def test_current_after_upgrade(self):
        """Current revision should be set after upgrade."""
        run_alembic("upgrade", "head")
        result = run_alembic("current")
        assert result.returncode == 0
        # Should show a revision hash
        output = result.stdout.strip()
        assert len(output) > 0, "Current should show revision after upgrade"


class TestMigrationDowngrade:
    def test_downgrade_and_reupgrade(self):
        """Downgrade to base and re-upgrade."""
        # First upgrade
        run_alembic("upgrade", "head")

        # Downgrade to base
        result = run_alembic("downgrade", "base")
        assert result.returncode == 0, f"downgrade failed: {result.stderr}"

        # Re-upgrade
        result = run_alembic("upgrade", "head")
        assert result.returncode == 0


class TestMigrationHistory:
    def test_history_after_upgrade(self):
        """History should show at least one migration."""
        run_alembic("upgrade", "head")
        result = run_alembic("history")
        assert result.returncode == 0
        assert "initial schema" in result.stdout.lower() or len(result.stdout.strip()) > 0


class TestMigrationStamp:
    def test_stamp_head(self):
        """Stamp database with head revision."""
        result = run_alembic("stamp", "head")
        assert result.returncode == 0

        # Verify current revision is set
        current = run_alembic("current")
        assert current.stdout.strip() != ""


class TestMigrateScript:
    def test_migrate_script_exists(self):
        """The migrate script should exist."""
        path = os.path.join(PROJECT_ROOT, "scripts", "migrate.py")
        assert os.path.exists(path)

    def test_migrate_script_has_commands(self):
        """The migrate script should define all migration commands."""
        import ast
        with open(os.path.join(PROJECT_ROOT, "scripts", "migrate.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        for cmd in ["cmd_upgrade", "cmd_downgrade", "cmd_generate", "cmd_history", "cmd_current", "cmd_pending", "cmd_stamp", "cmd_check"]:
            assert cmd in func_names, f"Missing command function: {cmd}"
