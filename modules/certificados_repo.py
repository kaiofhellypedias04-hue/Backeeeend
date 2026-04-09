from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .db import get_conn
from .settings import get_settings


def _db_enabled() -> bool:
    return bool(get_settings().normalized_database_url)


def garantir_schema_nfse_certificados() -> None:
    if not _db_enabled():
        return
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nfse_certificados (
              alias TEXT PRIMARY KEY,
              pfx_path TEXT,
              storage_provider TEXT,
              storage_bucket TEXT,
              storage_path TEXT,
              original_filename TEXT,
              created_at TIMESTAMP NOT NULL DEFAULT now(),
              updated_at TIMESTAMP NOT NULL DEFAULT now()
            );
            """
        )


def listar_certificados() -> list[dict]:
    if not _db_enabled():
        return []
    garantir_schema_nfse_certificados()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT alias, pfx_path, storage_provider, storage_bucket, storage_path, original_filename
            FROM nfse_certificados
            ORDER BY alias
            """
        ).fetchall()

    items: list[dict] = []
    for row in rows:
        item = {"alias": (row.get("alias") or "").strip()}
        pfx_path = (row.get("pfx_path") or "").strip()
        storage_path = (row.get("storage_path") or "").strip()
        if not item["alias"] or (not pfx_path and not storage_path):
            continue
        if pfx_path:
            item["pfxPath"] = pfx_path
        if row.get("storage_provider"):
            item["storage_provider"] = row.get("storage_provider")
        if row.get("storage_bucket"):
            item["storage_bucket"] = row.get("storage_bucket")
        if storage_path:
            item["storage_path"] = storage_path
        if row.get("original_filename"):
            item["original_filename"] = row.get("original_filename")
        items.append(item)
    return items


def obter_certificado(alias: str) -> Optional[dict]:
    alias_n = (alias or "").strip()
    if not _db_enabled() or not alias_n:
        return None
    garantir_schema_nfse_certificados()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT alias, pfx_path, storage_provider, storage_bucket, storage_path, original_filename
            FROM nfse_certificados
            WHERE alias = %s
            """,
            (alias_n,),
        ).fetchone()
    if not row:
        return None
    return next(iter([item for item in listar_certificados() if item.get("alias") == alias_n]), None)


def upsert_certificado(
    alias: str,
    pfx_path: str,
    *,
    storage_provider: str | None = None,
    storage_bucket: str | None = None,
    storage_path: str | None = None,
    original_filename: str | None = None,
) -> None:
    if not _db_enabled():
        raise RuntimeError("Banco de dados nao configurado para persistencia de certificados.")
    garantir_schema_nfse_certificados()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO nfse_certificados (
              alias, pfx_path, storage_provider, storage_bucket, storage_path, original_filename, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (alias) DO UPDATE SET
              pfx_path = EXCLUDED.pfx_path,
              storage_provider = EXCLUDED.storage_provider,
              storage_bucket = EXCLUDED.storage_bucket,
              storage_path = EXCLUDED.storage_path,
              original_filename = EXCLUDED.original_filename,
              updated_at = now()
            """,
            (alias, pfx_path, storage_provider, storage_bucket, storage_path, original_filename),
        )


def remover_certificado(alias: str) -> None:
    if not _db_enabled():
        raise RuntimeError("Banco de dados nao configurado para persistencia de certificados.")
    garantir_schema_nfse_certificados()
    with get_conn() as conn:
        conn.execute("DELETE FROM nfse_certificados WHERE alias = %s", (alias,))


def migrar_certificados_legados(certs_json_path: str | Path) -> int:
    if not _db_enabled():
        return 0
    garantir_schema_nfse_certificados()
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM nfse_certificados").fetchone()
    if row and int(row.get("total") or 0) > 0:
        return 0

    path = Path(certs_json_path)
    if not path.exists():
        legacy_path = get_settings().project_root / "certs.json"
        if legacy_path.exists():
            path = legacy_path
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0

    migrated = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        alias = (item.get("alias") or "").strip()
        pfx_path = (item.get("pfxPath") or "").strip()
        storage_path = (item.get("storage_path") or "").strip()
        if not alias or (not pfx_path and not storage_path):
            continue
        upsert_certificado(
            alias,
            pfx_path,
            storage_provider=item.get("storage_provider"),
            storage_bucket=item.get("storage_bucket"),
            storage_path=storage_path or None,
            original_filename=item.get("original_filename"),
        )
        migrated += 1
    return migrated
