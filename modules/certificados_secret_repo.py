from __future__ import annotations

from typing import Optional

from .db import get_conn
from .settings import get_settings


def _db_enabled() -> bool:
    return bool(get_settings().normalized_database_url)


def garantir_schema_nfse_certificados_segredos() -> None:
    if not _db_enabled():
        return
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nfse_certificados_segredos (
              alias TEXT PRIMARY KEY,
              cert_password TEXT NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT now(),
              updated_at TIMESTAMP NOT NULL DEFAULT now()
            );
            """
        )


def get_cert_password_db(alias: str) -> Optional[str]:
    alias_n = (alias or "").strip()
    if not _db_enabled() or not alias_n:
        return None
    garantir_schema_nfse_certificados_segredos()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT cert_password
            FROM nfse_certificados_segredos
            WHERE alias = %s
            """,
            (alias_n,),
        ).fetchone()
    if not row:
        return None
    return row.get("cert_password")


def set_cert_password_db(alias: str, password: str) -> None:
    alias_n = (alias or "").strip()
    if not _db_enabled() or not alias_n:
        raise RuntimeError("Banco de dados nao configurado para senha persistente de certificados.")
    garantir_schema_nfse_certificados_segredos()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO nfse_certificados_segredos (alias, cert_password, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (alias) DO UPDATE SET
              cert_password = EXCLUDED.cert_password,
              updated_at = now()
            """,
            (alias_n, password),
        )


def delete_cert_password_db(alias: str) -> None:
    alias_n = (alias or "").strip()
    if not _db_enabled() or not alias_n:
        return
    garantir_schema_nfse_certificados_segredos()
    with get_conn() as conn:
        conn.execute("DELETE FROM nfse_certificados_segredos WHERE alias = %s", (alias_n,))
