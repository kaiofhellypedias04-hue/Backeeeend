from __future__ import annotations

import os
import re
from typing import Any, List, Optional

from .db import get_conn
from .schemas import TipoArquivoEnum, ArquivoResponse


def garantir_schema_nfse_processo_arquivos():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nfse_processo_arquivos (
          id BIGSERIAL PRIMARY KEY,
          processo_id UUID NOT NULL REFERENCES nfse_processos(id) ON DELETE CASCADE,
          tipo_arquivo TEXT NOT NULL CHECK (tipo_arquivo IN ('pdf', 'xml', 'relatorio')),
          nome_arquivo TEXT NOT NULL,
          storage_key TEXT,
          caminho_local TEXT,
          content_type TEXT,
          tamanho_bytes BIGINT,
          competencia TEXT,
          created_at TIMESTAMP DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_nfse_arquivos_processo_tipo ON nfse_processo_arquivos (processo_id, tipo_arquivo);
        CREATE INDEX IF NOT EXISTS idx_nfse_arquivos_competencia ON nfse_processo_arquivos (competencia);
        """)


def _uuid_to_str(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _row_to_arquivo_response(row) -> ArquivoResponse:
    data = dict(row)
    data["processo_id"] = _uuid_to_str(data.get("processo_id"))
    return ArquivoResponse(**data)


def registrar_arquivo_processo(
    processo_id: str,
    tipo: TipoArquivoEnum,
    nome_arquivo: str,
    storage_key: Optional[str] = None,
    caminho_local: Optional[str] = None,
    content_type: Optional[str] = None,
    tamanho_bytes: Optional[int] = None,
    competencia: Optional[str] = None
):
    garantir_schema_nfse_processo_arquivos()

    tipo_db = tipo.value if hasattr(tipo, "value") else str(tipo)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO nfse_processo_arquivos (
              processo_id, tipo_arquivo, nome_arquivo, storage_key, caminho_local,
              content_type, tamanho_bytes, competencia
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            processo_id,
            tipo_db,
            nome_arquivo,
            storage_key,
            caminho_local,
            content_type,
            tamanho_bytes,
            competencia
        ))


def listar_arquivos_processo(processo_id: str, tipo: Optional[TipoArquivoEnum] = None) -> List[ArquivoResponse]:
    garantir_schema_nfse_processo_arquivos()

    params = [processo_id]
    where = ""

    if tipo:
        tipo_db = tipo.value if hasattr(tipo, "value") else str(tipo)
        where = " AND tipo_arquivo = %s"
        params.append(tipo_db)

    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT *
            FROM nfse_processo_arquivos
            WHERE processo_id = %s {where}
            ORDER BY created_at DESC
        """, params).fetchall()

    return [_row_to_arquivo_response(row) for row in rows]


def obter_arquivo_processo(arquivo_id: int) -> Optional[ArquivoResponse]:
    garantir_schema_nfse_processo_arquivos()

    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM nfse_processo_arquivos WHERE id = %s
        """, (arquivo_id,)).fetchone()

        if not row:
            return None

        return _row_to_arquivo_response(row)


def _normalize_lookup_text(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = str(value).strip().lower().replace("\\", "/")
    normalized = os.path.basename(normalized)
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _build_tokens(*values: Optional[str]) -> List[str]:
    tokens: List[str] = []
    seen = set()
    for value in values:
        token = _normalize_lookup_text(value)
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def localizar_documento_nota(
    tipo: TipoArquivoEnum,
    processo_id: Optional[str] = None,
    arquivo_origem: Optional[str] = None,
    numero_documento: Optional[str] = None,
    chave_nfse: Optional[str] = None,
) -> Optional[ArquivoResponse]:
    garantir_schema_nfse_processo_arquivos()

    tipo_db = tipo.value if hasattr(tipo, "value") else str(tipo)
    arquivo_tokens = _build_tokens(arquivo_origem)
    numero_tokens = _build_tokens(numero_documento)
    chave_tokens = _build_tokens(chave_nfse)
    tokens = arquivo_tokens + [t for t in numero_tokens + chave_tokens if t not in arquivo_tokens]

    if not processo_id and not tokens:
        return None

    def _query_candidate(process_scope_only: bool) -> Optional[ArquivoResponse]:
        where_clauses = ["tipo_arquivo = %s"]
        params: List[Any] = [tipo_db]
        score_exprs: List[str] = []
        score_params: List[Any] = []

        if processo_id:
            where_clauses.append("processo_id = %s")
            params.append(processo_id)
        elif process_scope_only:
            return None

        if arquivo_tokens:
            score_exprs.append(
                """
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM unnest(%s::text[]) AS token
                    WHERE regexp_replace(lower(coalesce(nome_arquivo, '')), '[^a-z0-9]+', '', 'g') = token
                       OR regexp_replace(lower(coalesce(storage_key, '')), '[^a-z0-9]+', '', 'g') LIKE '%%' || token || '%%'
                       OR regexp_replace(lower(coalesce(caminho_local, '')), '[^a-z0-9]+', '', 'g') LIKE '%%' || token || '%%'
                ) THEN 100 ELSE 0 END
                """
            )
            score_params.append(arquivo_tokens)

        if numero_tokens:
            score_exprs.append(
                """
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM unnest(%s::text[]) AS token
                    WHERE regexp_replace(lower(coalesce(nome_arquivo, '')), '[^a-z0-9]+', '', 'g') LIKE '%%' || token || '%%'
                       OR regexp_replace(lower(coalesce(storage_key, '')), '[^a-z0-9]+', '', 'g') LIKE '%%' || token || '%%'
                       OR regexp_replace(lower(coalesce(caminho_local, '')), '[^a-z0-9]+', '', 'g') LIKE '%%' || token || '%%'
                ) THEN 30 ELSE 0 END
                """
            )
            score_params.append(numero_tokens)

        if chave_tokens:
            score_exprs.append(
                """
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM unnest(%s::text[]) AS token
                    WHERE regexp_replace(lower(coalesce(nome_arquivo, '')), '[^a-z0-9]+', '', 'g') LIKE '%%' || token || '%%'
                       OR regexp_replace(lower(coalesce(storage_key, '')), '[^a-z0-9]+', '', 'g') LIKE '%%' || token || '%%'
                       OR regexp_replace(lower(coalesce(caminho_local, '')), '[^a-z0-9]+', '', 'g') LIKE '%%' || token || '%%'
                ) THEN 60 ELSE 0 END
                """
            )
            score_params.append(chave_tokens)

        score_sql = " + ".join(score_exprs) if score_exprs else "0"
        if tokens:
            where_clauses.append(f"({score_sql}) > 0")

        with get_conn() as conn:
            row = conn.execute(
                f"""
                SELECT *,
                       ({score_sql}) AS score
                FROM nfse_processo_arquivos
                WHERE {' AND '.join(where_clauses)}
                ORDER BY score DESC, created_at DESC
                LIMIT 1
                """,
                [*params, *score_params, *score_params],
            ).fetchone()

        return _row_to_arquivo_response(row) if row else None

    candidato = _query_candidate(process_scope_only=True)
    if candidato:
        return candidato

    if processo_id:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM nfse_processo_arquivos
                WHERE processo_id = %s AND tipo_arquivo = %s
                ORDER BY created_at DESC
                """,
                (processo_id, tipo_db),
            ).fetchall()
        if len(rows) == 1:
            return _row_to_arquivo_response(rows[0])

    return _query_candidate(process_scope_only=False)
