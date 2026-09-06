"""Tests for DSN password URL-encoding in api.application.services.sql_leg.build_dsn().

Ensures passwords with special characters (such as '@' and ':') do not corrupt
the DSN host/port/user parsing in asyncpg or urllib.parse, while safe passwords
remain unchanged.
"""

from __future__ import annotations

import urllib.parse
from unittest.mock import patch

import asyncpg.connect_utils
import pytest

from api.application.services.sql_leg import build_dsn


def test_build_dsn_encodes_special_password() -> None:
    """A password containing '@' and ':' produces a DSN whose netloc parses correctly."""
    mock_cfg = {
        "postgres_host": "aws-1-ap-southeast-1.pooler.supabase.com",
        "postgres_port": 5432,
        "postgres_user": "postgres.wlvxhmzjyomtzwboehsd",
        "postgres_password": "p@ss:word@@123",
        "postgres_database": "postgres",
    }

    with patch("api.application.services.sql_leg.get_cfg", side_effect=lambda k, d=None: mock_cfg.get(k, d)):
        dsn = build_dsn()

    # Verify with urllib.parse.urlsplit
    parsed = urllib.parse.urlsplit(dsn)
    assert parsed.scheme == "postgresql"
    assert parsed.username == "postgres.wlvxhmzjyomtzwboehsd"
    assert urllib.parse.unquote(parsed.password or "") == "p@ss:word@@123"
    assert parsed.hostname == "aws-1-ap-southeast-1.pooler.supabase.com"
    assert parsed.port == 5432
    assert parsed.path == "/postgres"

    # Verify with asyncpg's internal DSN parser (no network connection needed)
    addrs, params = asyncpg.connect_utils._parse_connect_dsn_and_args(
        dsn=dsn,
        host=None,
        port=None,
        user=None,
        password=None,
        passfile=None,
        database=None,
        ssl=None,
        service=None,
        servicefile=None,
        direct_tls=None,
        server_settings=None,
        target_session_attrs=None,
        krbsrvname=None,
        gsslib=None,
    )
    assert addrs == [("aws-1-ap-southeast-1.pooler.supabase.com", 5432)]
    assert params.user == "postgres.wlvxhmzjyomtzwboehsd"
    assert params.password == "p@ss:word@@123"
    assert params.database == "postgres"


def test_build_dsn_plain_password_unchanged() -> None:
    """A password without special characters remains unchanged (quote is idempotent for safe chars)."""
    mock_cfg = {
        "postgres_host": "localhost",
        "postgres_port": 5432,
        "postgres_user": "ragre",
        "postgres_password": "plainpassword123",
        "postgres_database": "ragre",
    }

    with patch("api.application.services.sql_leg.get_cfg", side_effect=lambda k, d=None: mock_cfg.get(k, d)):
        dsn = build_dsn()

    assert dsn == "postgresql://ragre:plainpassword123@localhost:5432/ragre"

    parsed = urllib.parse.urlsplit(dsn)
    assert parsed.username == "ragre"
    assert parsed.password == "plainpassword123"
    assert parsed.hostname == "localhost"
    assert parsed.port == 5432
    assert parsed.path == "/ragre"


def test_settings_pg_dsn_encodes_special_password() -> None:
    """Settings.pg_dsn percent-encodes '@' in the password so asyncpg parses the host correctly.

    The raw password must still be available via settings.postgres_password.
    """
    from api.infrastructure.config.config import Settings

    settings = Settings(
        postgres_host="aws-1-ap-southeast-1.pooler.supabase.com",
        postgres_port=5432,
        postgres_user="postgres.wlvxhmzjyomtzwboehsd",
        postgres_password="p@ss:word@@123",
        postgres_database="postgres",
    )

    dsn = settings.pg_dsn

    # Raw password unchanged
    assert settings.postgres_password == "p@ss:word@@123"

    # urllib parse
    parsed = urllib.parse.urlsplit(dsn)
    assert parsed.scheme == "postgresql"
    assert parsed.username == "postgres.wlvxhmzjyomtzwboehsd"
    assert urllib.parse.unquote(parsed.password or "") == "p@ss:word@@123"
    assert parsed.hostname == "aws-1-ap-southeast-1.pooler.supabase.com"
    assert parsed.port == 5432
    assert parsed.path == "/postgres"

    # asyncpg parse
    addrs, params = asyncpg.connect_utils._parse_connect_dsn_and_args(
        dsn=dsn,
        host=None,
        port=None,
        user=None,
        password=None,
        passfile=None,
        database=None,
        ssl=None,
        service=None,
        servicefile=None,
        direct_tls=None,
        server_settings=None,
        target_session_attrs=None,
        krbsrvname=None,
        gsslib=None,
    )
    assert addrs == [("aws-1-ap-southeast-1.pooler.supabase.com", 5432)]
    assert params.user == "postgres.wlvxhmzjyomtzwboehsd"
    assert params.password == "p@ss:word@@123"
    assert params.database == "postgres"
