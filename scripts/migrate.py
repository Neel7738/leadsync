#!/usr/bin/env python3
"""
Database migration helper for the Sales Follow-Up Agent.

Wraps Alembic commands with convenient shortcuts.

Usage:
    # Apply all pending migrations
    python scripts/migrate.py upgrade

    # Rollback last migration
    python scripts/migrate.py downgrade

    # Show migration history
    python scripts/migrate.py history

    # Show current revision
    python scripts/migrate.py current

    # Generate new migration (autogenerate from model changes)
    python scripts/migrate.py generate "add new column to conversations"

    # Stamp database with a specific revision (without running migrations)
    python scripts/migrate.py stamp head

    # Show pending migrations
    python scripts/migrate.py pending
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_DIR = os.path.join(PROJECT_ROOT, "alembic")


def run_alembic(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    """Run an alembic command."""
    cmd = [sys.executable, "-m", "alembic"] + list(args)
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT

    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT, env=env)
    else:
        return subprocess.run(cmd, cwd=PROJECT_ROOT, env=env)


def cmd_upgrade(args):
    """Apply pending migrations."""
    target = args.revision or "head"
    print(f"⬆️  Upgrading database to: {target}")

    result = run_alembic("upgrade", target)
    if result.returncode == 0:
        print("✅ Migration complete")
    else:
        print(f"❌ Migration failed")
        if result.stderr:
            print(result.stderr)
        sys.exit(1)


def cmd_downgrade(args):
    """Rollback migrations."""
    target = args.revision or "-1"
    print(f"⬇️  Downgrading database to: {target}")

    result = run_alembic("downgrade", target)
    if result.returncode == 0:
        print("✅ Downgrade complete")
    else:
        print(f"❌ Downgrade failed")
        if result.stderr:
            print(result.stderr)
        sys.exit(1)


def cmd_generate(args):
    """Generate a new migration from model changes."""
    message = args.message
    if not message:
        message = f"auto_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    print(f"📝 Generating migration: {message}")

    result = run_alembic("revision", "--autogenerate", "-m", message, capture=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f"❌ Generation failed: {result.stderr}")
        sys.exit(1)

    # Extract the new file path from output
    for line in result.stdout.split("\n"):
        if "Generating" in line and ".py" in line:
            path = line.split("Generating")[-1].strip().rstrip(" ... done")
            print(f"✅ Created: {os.path.basename(path)}")
            break


def cmd_history(args):
    """Show migration history."""
    result = run_alembic("history", "--verbose" if args.verbose else "")
    print(result.stdout)


def cmd_current(args):
    """Show current revision."""
    result = run_alembic("current", "--verbose" if args.verbose else "")
    print(result.stdout)


def cmd_pending(args):
    """Show pending migrations."""
    # Get current and head
    current = run_alembic("current", capture=True)
    heads = run_alembic("heads", capture=True)

    print("📋 Migration status:")
    print(f"   Current: {current.stdout.strip() or '(no migrations applied)'}")
    print(f"   Head:    {heads.stdout.strip() or '(no migrations)'}")

    # Show what would be applied
    result = run_alembic("history", capture=True)
    if result.stdout.strip():
        print(f"\n📜 All migrations:")
        print(result.stdout)
    else:
        print("\n   No migrations found.")


def cmd_stamp(args):
    """Stamp database with a revision without running migrations."""
    target = args.revision or "head"
    print(f"📌 Stamping database to: {target}")

    result = run_alembic("stamp", target)
    if result.returncode == 0:
        print("✅ Stamped successfully")
    else:
        print(f"❌ Stamp failed: {result.stderr}")
        sys.exit(1)


def cmd_check(args):
    """Check for model changes without generating a migration."""
    print("🔍 Checking for model changes...")

    result = run_alembic("revision", "--autogenerate", "--sql", "-m", "check_only", capture=True)

    # Check if any tables were detected
    if "No changes detected" in result.stdout:
        print("✅ No model changes detected — schema is up to date")
    else:
        print("⚠️  Model changes detected! Run 'migrate generate' to create a migration.")
        # Show what was detected
        for line in result.stdout.split("\n"):
            if "Detected" in line:
                print(f"   {line.strip()}")


def main():
    parser = argparse.ArgumentParser(
        description="Database migration helper for Sales Follow-Up Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", help="Migration command")

    # Upgrade
    p_up = sub.add_parser("upgrade", help="Apply pending migrations")
    p_up.add_argument("revision", nargs="?", default="head", help="Target revision (default: head)")
    p_up.set_defaults(func=cmd_upgrade)

    # Downgrade
    p_down = sub.add_parser("downgrade", help="Rollback migrations")
    p_down.add_argument("revision", nargs="?", default="-1", help="Target revision (default: -1)")
    p_down.set_defaults(func=cmd_downgrade)

    # Generate
    p_gen = sub.add_parser("generate", help="Generate new migration from model changes")
    p_gen.add_argument("message", nargs="?", help="Migration description")
    p_gen.set_defaults(func=cmd_generate)

    # History
    p_hist = sub.add_parser("history", help="Show migration history")
    p_hist.add_argument("-v", "--verbose", action="store_true", help="Show details")
    p_hist.set_defaults(func=cmd_history)

    # Current
    p_curr = sub.add_parser("current", help="Show current revision")
    p_curr.add_argument("-v", "--verbose", action="store_true", help="Show details")
    p_curr.set_defaults(func=cmd_current)

    # Pending
    p_pending = sub.add_parser("pending", help="Show pending migrations")
    p_pending.set_defaults(func=cmd_pending)

    # Stamp
    p_stamp = sub.add_parser("stamp", help="Stamp DB with revision (no-op migration)")
    p_stamp.add_argument("revision", nargs="?", default="head", help="Revision to stamp")
    p_stamp.set_defaults(func=cmd_stamp)

    # Check
    p_check = sub.add_parser("check", help="Check for model changes")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
