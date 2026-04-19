#!/usr/bin/env python3
"""
Reset the password for a super_admin user in the LockBot platform.

Usage:
    python tools/reset_super_admin_password.py [--username USERNAME] [--password PASSWORD] [--dry-run]

If --password is not provided, a random password will be generated and printed.
If --dry-run is set, the script only shows what would happen without making any changes.
"""

import argparse
import secrets
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lockbot.backend.app.auth.models import User  # noqa: E402
from lockbot.backend.app.auth.router import _hash_password  # noqa: E402
from lockbot.backend.app.config import BASE_DIR  # noqa: E402
from lockbot.backend.app.database import Base  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def get_db_url() -> str:
    """Resolve database URL, respecting DATA_DIR env var."""
    import os

    data_dir = os.environ.get("DATA_DIR", str(BASE_DIR / "data"))
    db_path = Path(data_dir)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    db_path.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path / 'lockbot.db'}"


def main():
    parser = argparse.ArgumentParser(description="Reset a super_admin password for LockBot")
    parser.add_argument(
        "--username", default=None, help="Target super_admin username (default: first super_admin found)"
    )
    parser.add_argument("--password", default=None, help="New password (auto-generated if not provided)")
    parser.add_argument("--db", default=None, help="Custom database URL (default: auto-detect)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without actually writing to the database",
    )
    args = parser.parse_args()

    db_url = args.db or get_db_url()
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    try:
        # Find target user
        if args.username:
            user = db.query(User).filter(User.username == args.username).first()
            if not user:
                print(f"Error: User '{args.username}' not found.")
                sys.exit(1)
            if user.role != "super_admin":
                print(f"Error: User '{args.username}' is not a super_admin (role={user.role}).")
                print("This tool only resets passwords for super_admin accounts.")
                sys.exit(1)
        else:
            user = db.query(User).filter(User.role == "super_admin").first()
            if not user:
                print("Error: No super_admin user found in the database.")
                print("Tip: Use tools/create_super_admin.py to create one.")
                sys.exit(1)

        new_password = args.password or secrets.token_urlsafe(12)

        print(f"{'[DRY-RUN] ' if args.dry_run else ''}Target user:")
        print(f"  ID:       {user.id}")
        print(f"  Username: {user.username}")
        print(f"  Email:    {user.email}")
        print(f"  Role:     {user.role}")
        print()

        if args.dry_run:
            print("[DRY-RUN] The following changes would be applied:")
            print(f"  password_hash  → <bcrypt hash of '{new_password}'>")
            print(f"  token_version  → {user.effective_token_version + 1}  (invalidates all active sessions)")
            print("  must_change_password → True")
            print()
            print("[DRY-RUN] No changes were made.")
            return

        # Apply the reset
        user.password_hash = _hash_password(new_password)
        user.token_version = user.effective_token_version + 1  # invalidate existing tokens
        user.must_change_password = True
        db.commit()

        print("✓ Password reset successfully!")
        print(f"  New password: {new_password}")
        print("  ⚠️  All active sessions for this user have been invalidated.")
        print("  ⚠️  User will be required to change password on next login.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
