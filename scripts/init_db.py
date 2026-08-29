#!/usr/bin/env python
"""Initialize or reset the database.

Usage:
    python scripts/init_db.py          # Apply migrations (safe, idempotent)
    python scripts/init_db.py --reset  # Drop and recreate all tables (DESTRUCTIVE)
    python scripts/init_db.py --legacy # Use old init_db() without Alembic
"""

import subprocess
import sys
import os

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def use_alembic():
    """Try to apply migrations via Alembic."""
    print("📦 Applying database migrations via Alembic...")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(result.stdout)
        print("✅ Database migrations applied.")
        return True
    else:
        print(f"⚠️  Alembic failed: {result.stderr[:300]}")
        return False


def use_legacy():
    """Fallback: use init_db() to create tables directly."""
    from core.database import init_db, reset_db, get_engine
    from core.database import models  # noqa: F401 — register models

    init_db()
    print("✅ Database tables created/verified (legacy mode).")

    # Show tables
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            __import__('sqlalchemy').text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        tables = [row[0] for row in result]
        print(f"\nTables ({len(tables)}):")
        for t in sorted(tables):
            if not t.startswith("sqlite_"):
                print(f"  - {t}")


def reset_database():
    """Drop and recreate all tables."""
    from core.database import reset_db
    from core.database import models  # noqa: F401

    print("⚠️  DROPPING all tables and recreating...")
    confirm = input("Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return
    reset_db()
    print("✅ Database reset complete.")

    # Re-apply migrations
    print("\n📦 Re-applying migrations...")
    use_alembic()


def main():
    if "--reset" in sys.argv:
        reset_database()
    elif "--legacy" in sys.argv:
        use_legacy()
    else:
        # Try Alembic first, fall back to legacy
        if not use_alembic():
            print("\nFalling back to legacy table creation...")
            use_legacy()


if __name__ == "__main__":
    main()
