# ============================================================
# FILE: back_end/secrets.py
# ============================================================
"""
PhoneBox — Credentials & Secrets
==================================
All passwords and connection credentials in one place, each with an
environment-variable override so production deployments never need to
touch source code.

Usage:
    from back_end.secrets import Secrets

Environment variables (override any default):
    PHONEBOX_DB_USER          PHONEBOX_DB_PASSWORD
    PHONEBOX_ADMIN_PASSWORD   PHONEBOX_FRONT_ADMIN_PASSWORD

DO NOT commit real production passwords here.
In production, set the environment variables and leave the defaults empty.
"""

import os


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


class Secrets:
    # ── Database credentials ──────────────────────────────
    # Used by both the sync (psycopg2) and async (asyncpg) pools.
    DB_USER     = _env("PHONEBOX_DB_USER",     "admin")
    DB_PASSWORD = _env("PHONEBOX_DB_PASSWORD", "admin")

    # ── Alarm / admin resolution password ────────────────
    # Checked by AlarmController.authenticate_admin() and
    # AdminOpsHandler._handle_session_start().
    ADMIN_PASSWORD = _env("PHONEBOX_ADMIN_PASSWORD", "admin")

    # ── Flutter front-end local admin password ────────────
    # Used by auth.dart AuthService.login().
    # Keep in sync with the Flutter build or move to a config endpoint.
    FRONT_ADMIN_PASSWORD = _env("PHONEBOX_FRONT_ADMIN_PASSWORD", "admin123")