#!/usr/bin/env python3
"""
Create a super_admin user for the LockBot platform.

Usage:
    python tools/create_super_admin.py [--username USERNAME] [--email EMAIL] [--password PASSWORD]

If --password is not provided, a random password will be generated and printed.
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
    parser = argparse.ArgumentParser(description="Create a super_admin user for LockBot")
    parser.add_argument("--username", default="admin", help="Admin username (default: admin)")
    parser.add_argument("--email", default="admin@lockbot.local", help="Admin email")
    parser.add_argument("--password", default=None, help="Password (auto-generated if not provided)")
    parser.add_argument("--db", default=None, help="Custom database URL (default: auto-detect)")
    args = parser.parse_args()

    db_url = args.db or get_db_url()
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    try:
        existing = db.query(User).filter(User.username == args.username).first()
        if existing:
            print(f"User '{args.username}' already exists (id={existing.id}, role={existing.role})")
            if existing.role == "super_admin":
                print("Already a super_admin, nothing to do.")
            else:
                existing.role = "super_admin"
                db.commit()
                print(f"Promoted '{args.username}' to super_admin.")
            db.close()
            return

        password = args.password or secrets.token_urlsafe(12)
        user = User(
            username=args.username,
            email=args.email,
            password_hash=_hash_password(password),
            role="super_admin",
            must_change_password=args.password is None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        print("✓ Super admin created successfully!")
        print(f"  Username: {user.username}")
        print(f"  Email:    {user.email}")
        print(f"  Password: {password}")
        if args.password is None:
            print("  ⚠️  Please change the password after first login (--must-change-password)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
