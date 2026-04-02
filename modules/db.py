from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .settings import get_settings


def get_database_url() -> str:
    settings = get_settings()
    if not settings.normalized_database_url:
        raise RuntimeError(
            "DATABASE_URL não está definido. "
            "Defina a variável no Render ou em um .env local."
        )
    return settings.normalized_database_url


def ensure_database_extensions() -> None:
    settings = get_settings()
    conn = psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
        connect_timeout=settings.db_connect_timeout,
        sslmode=settings.db_sslmode or "require",
    )
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn():
    settings = get_settings()
    conn = psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
        connect_timeout=settings.db_connect_timeout,
        sslmode=settings.db_sslmode or "require",
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
