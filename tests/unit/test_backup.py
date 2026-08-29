"""Tests for database backup and restore utilities."""

import gzip
import json
import os
import sqlite3
import tempfile

import pytest

from scripts.db_backup import (
    _file_checksum,
    _gzip_file,
    _gunzip_file,
    _is_sqlite,
    _is_postgres,
    _parse_pg_url,
    _rotate_backups,
    backup_sqlite,
    list_backups,
    restore_sqlite,
    verify_backup,
)


# ── Helpers ────────────────────────────────────────────────────


class TestHelpers:
    def test_file_checksum(self, tmp_path):
        path = str(tmp_path / "test.txt")
        with open(path, "w") as f:
            f.write("hello world")
        checksum = _file_checksum(path)
        assert len(checksum) == 64  # SHA-256 hex
        # Same content = same checksum
        assert checksum == _file_checksum(path)

    def test_gzip_roundtrip(self, tmp_path):
        src = str(tmp_path / "data.sql")
        dst = str(tmp_path / "data.sql.gz")
        restored = str(tmp_path / "restored.sql")

        # Use enough data that gzip actually compresses
        content = "SELECT * FROM conversations;\n" * 500
        with open(src, "w") as f:
            f.write(content)

        _gzip_file(src, dst)
        assert os.path.exists(dst)
        # Gzip should compress repetitive data
        assert os.path.getsize(dst) < os.path.getsize(src)

        _gunzip_file(dst, restored)
        with open(restored) as f:
            assert f.read() == content

    def test_is_sqlite(self):
        assert _is_sqlite("sqlite:///data.db") is True
        assert _is_sqlite("sqlite:///:memory:") is True
        assert _is_sqlite("postgresql://host/db") is False

    def test_is_postgres(self):
        assert _is_postgres("postgresql://host/db") is True
        assert _is_postgres("postgres://host/db") is True
        assert _is_postgres("sqlite:///data.db") is False

    def test_parse_pg_url(self):
        pg = _parse_pg_url("postgresql://user:pass@db.example.com:5432/sfa")
        assert pg["user"] == "user"
        assert pg["password"] == "pass"
        assert pg["host"] == "db.example.com"
        assert pg["port"] == "5432"
        assert pg["dbname"] == "sfa"

    def test_parse_pg_url_defaults(self):
        pg = _parse_pg_url("postgresql://localhost/mydb")
        assert pg["user"] == "postgres"
        assert pg["port"] == "5432"


# ── SQLite Backup/Restore ──────────────────────────────────────


class TestSQLiteBackupRestore:
    def _create_test_db(self, path: str) -> None:
        """Create a test SQLite database with data."""
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                source TEXT,
                raw_text TEXT,
                sentiment TEXT
            )
        """)
        conn.execute("INSERT INTO conversations VALUES ('c1', 'email', 'Hello world', 'positive')")
        conn.execute("INSERT INTO conversations VALUES ('c2', 'call', 'Meeting notes', 'neutral')")
        conn.commit()
        conn.close()

    def test_backup_creates_file(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        self._create_test_db(db_path)

        output_dir = str(tmp_path / "backups")
        os.makedirs(output_dir)

        result = backup_sqlite(f"sqlite:///{db_path}", output_dir, compress=False)
        assert result["status"] == "success"
        assert os.path.exists(result["backup_path"])
        assert result["size_bytes"] > 0
        assert len(result["checksum"]) == 64

    def test_backup_compressed(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        self._create_test_db(db_path)

        output_dir = str(tmp_path / "backups")
        os.makedirs(output_dir)

        result = backup_sqlite(f"sqlite:///{db_path}", output_dir, compress=True)
        assert result["status"] == "success"
        assert result["backup_path"].endswith(".gz")
        assert result.get("compressed") is True

    def test_restore_from_backup(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        self._create_test_db(db_path)

        output_dir = str(tmp_path / "backups")
        os.makedirs(output_dir)

        # Backup
        result = backup_sqlite(f"sqlite:///{db_path}", output_dir, compress=False)
        backup_path = result["backup_path"]

        # Wipe original
        os.remove(db_path)
        assert not os.path.exists(db_path)

        # Restore
        restore_result = restore_sqlite(backup_path, f"sqlite:///{db_path}")
        assert restore_result["status"] == "success"
        assert os.path.exists(db_path)

        # Verify data
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 2

    def test_backup_missing_file(self, tmp_path):
        output_dir = str(tmp_path / "backups")
        os.makedirs(output_dir)
        result = backup_sqlite("sqlite:///nonexistent.db", output_dir)
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()


# ── List Backups ───────────────────────────────────────────────


class TestListBackups:
    def test_list_empty_dir(self, tmp_path):
        d = str(tmp_path / "backups")
        os.makedirs(d)
        assert list_backups(d) == []

    def test_list_with_backups(self, tmp_path):
        d = str(tmp_path / "backups")
        os.makedirs(d)

        # Create some fake backup files
        for name in ["sfa_20240101_120000.sql", "sfa_20240102_120000.sql.gz", "sfa_20240101_120000_dedup_cache.json"]:
            with open(os.path.join(d, name), "w") as f:
                f.write("test")

        backups = list_backups(d)
        assert len(backups) == 3

        # SQL backup should be "database" type
        db_backups = [b for b in backups if b["type"] == "database"]
        assert len(db_backups) == 2

    def test_list_nonexistent_dir(self):
        assert list_backups("/nonexistent/path") == []


# ── Verify ─────────────────────────────────────────────────────


class TestVerify:
    def test_verify_valid_sql(self, tmp_path):
        path = str(tmp_path / "backup.sql")
        with open(path, "w") as f:
            f.write("-- Backup\nSELECT 1;\n")

        result = verify_backup(path)
        assert result["status"] == "valid"
        assert result["readable"] is True

    def test_verify_valid_gzip(self, tmp_path):
        sql_path = str(tmp_path / "backup.sql")
        gz_path = str(tmp_path / "backup.sql.gz")

        with open(sql_path, "w") as f:
            f.write("-- Backup\nSELECT 1;\n")
        _gzip_file(sql_path, gz_path)

        result = verify_backup(gz_path)
        assert result["status"] == "valid"
        assert result["gzip_valid"] is True

    def test_verify_missing_file(self):
        result = verify_backup("/nonexistent/file.sql")
        assert result["status"] == "error"

    def test_verify_corrupt_gzip(self, tmp_path):
        path = str(tmp_path / "corrupt.gz")
        with open(path, "wb") as f:
            f.write(b"this is not gzip")

        result = verify_backup(path)
        assert result["status"] == "error"
        assert result.get("gzip_valid") is False


# ── Rotation ───────────────────────────────────────────────────


class TestRotation:
    def test_rotate_keeps_recent(self, tmp_path):
        d = str(tmp_path / "backups")
        os.makedirs(d)

        for i in range(5):
            with open(os.path.join(d, f"sfa_2024010{i}_120000.sql"), "w") as f:
                f.write(f"backup {i}")

        removed = _rotate_backups(d, keep_count=3)
        assert len(removed) == 2

        remaining = list_backups(d)
        assert len(remaining) == 3

    def test_rotate_noop_when_under_limit(self, tmp_path):
        d = str(tmp_path / "backups")
        os.makedirs(d)

        for i in range(2):
            with open(os.path.join(d, f"sfa_2024010{i}_120000.sql"), "w") as f:
                f.write(f"backup {i}")

        removed = _rotate_backups(d, keep_count=5)
        assert len(removed) == 0
