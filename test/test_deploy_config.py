"""Settings hosts hand us: Render's postgres:// URL and the port it picks."""
from db import _async_url


def test_plain_postgres_urls_become_asyncpg():
    assert _async_url("postgres://u:p@h:5432/d") == "postgresql+asyncpg://u:p@h:5432/d"
    assert _async_url("postgresql://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"


def test_tls_is_spelled_the_asyncpg_way_and_asyncpg_urls_are_kept():
    assert _async_url("postgresql://u:p@h/d?sslmode=require") == "postgresql+asyncpg://u:p@h/d?ssl=require"
    assert _async_url("postgresql+asyncpg://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
    assert _async_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"


def test_the_container_listens_where_the_host_says():
    from pathlib import Path
    assert "--port ${PORT:-8001}" in (Path(__file__).parents[1] / "Dockerfile").read_text()
