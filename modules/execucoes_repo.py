from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List

from .db import get_conn
from psycopg.types.json import Jsonb


def garantir_schema_nfse_execucoes():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nfse_execucoes (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          job_id TEXT,
          processo_id UUID,
          payload_json JSONB,
          status TEXT DEFAULT 'queued',
          created_at TIMESTAMP DEFAULT now(),
          started_at TIMESTAMP,
          finished_at TIMESTAMP,
          error TEXT,
          traceback TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_nfse_execucoes_job ON nfse_execucoes (job_id);
        CREATE INDEX IF NOT EXISTS idx_nfse_execucoes_processo ON nfse_execucoes (processo_id);
        """)


def criar_execucao(job_id: str, processo_id: str, payload: Dict[str, Any]) -> str:
    execucao_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO nfse_execucoes (id, job_id, processo_id, payload_json, status)
            VALUES (%s, %s, %s, %s, 'queued')
            """,
            (execucao_id, job_id, processo_id, Jsonb(payload)),
        )
    return execucao_id


def atualizar_status_execucao(job_id: str, status: str, started_at: Optional[datetime] = None, finished_at: Optional[datetime] = None, error: Optional[str] = None, traceback: Optional[str] = None):
    set_parts = ["status = %s"]
    params: List[Any] = [status]
    if started_at:
        set_parts.append("started_at = COALESCE(started_at, %s)")
        params.append(started_at)
    if finished_at:
        set_parts.append("finished_at = %s")
        params.append(finished_at)
    if error:
        set_parts.append("error = %s")
        params.append(error)
    if traceback:
        set_parts.append("traceback = %s")
        params.append(traceback)
    params.append(job_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE nfse_execucoes SET {', '.join(set_parts)} WHERE job_id = %s", params)


def _row_to_dict(row):
    try:
        return dict(row)
    except Exception:
        return row


def listar_execucoes(page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    offset = (page - 1) * page_size
    with get_conn() as conn:
        count_row = conn.execute("SELECT COUNT(DISTINCT job_id) AS total FROM nfse_execucoes").fetchone()
        total = int((count_row["total"] if count_row else 0) or 0)
        rows = conn.execute(
            """
            WITH exec_agg AS (
                SELECT
                    e.job_id,
                    MIN(e.created_at) AS created_at,
                    MIN(e.started_at) AS started_at,
                    MAX(e.finished_at) AS finished_at,
                    ARRAY_REMOVE(ARRAY_AGG(DISTINCT p.cert_alias), NULL) AS aliases,
                    COUNT(*) AS total_processos,
                    COUNT(*) FILTER (WHERE p.status = 'completed') AS processos_concluidos,
                    COUNT(*) FILTER (WHERE p.status = 'failed') AS processos_falhos,
                    CASE
                        WHEN COUNT(*) FILTER (WHERE p.status = 'running') > 0 THEN 'running'
                        WHEN COUNT(*) FILTER (WHERE p.status = 'failed') > 0 THEN 'failed'
                        WHEN COUNT(*) FILTER (WHERE p.status = 'completed') = COUNT(*) AND COUNT(*) > 0 THEN 'completed'
                        ELSE COALESCE(MAX(e.status), 'queued')
                    END AS status,
                    MAX(e.error) FILTER (WHERE e.error IS NOT NULL) AS error_message
                FROM nfse_execucoes e
                LEFT JOIN nfse_processos p ON p.id = e.processo_id
                GROUP BY e.job_id
            )
            SELECT
                a.job_id,
                a.created_at,
                a.started_at,
                a.finished_at,
                a.aliases,
                a.total_processos,
                a.processos_concluidos,
                a.processos_falhos,
                a.status,
                a.error_message,
                (
                    SELECT e2.payload_json
                    FROM nfse_execucoes e2
                    WHERE e2.job_id = a.job_id
                    ORDER BY e2.created_at ASC
                    LIMIT 1
                ) AS payload_json
            FROM exec_agg a
            ORDER BY a.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (page_size, offset),
        ).fetchall()
    return {
        "items": [_row_to_dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def obter_execucao(job_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                e.job_id,
                MIN(e.created_at) AS created_at,
                MIN(e.started_at) AS started_at,
                MAX(e.finished_at) AS finished_at,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT p.cert_alias), NULL) AS aliases,
                COUNT(*) AS total_processos,
                COUNT(*) FILTER (WHERE p.status = 'completed') AS processos_concluidos,
                COUNT(*) FILTER (WHERE p.status = 'failed') AS processos_falhos,
                CASE
                    WHEN COUNT(*) FILTER (WHERE p.status = 'running') > 0 THEN 'running'
                    WHEN COUNT(*) FILTER (WHERE p.status = 'failed') > 0 THEN 'failed'
                    WHEN COUNT(*) FILTER (WHERE p.status = 'completed') = COUNT(*) AND COUNT(*) > 0 THEN 'completed'
                    ELSE COALESCE(MAX(e.status), 'queued')
                END AS status,
                MAX(e.error) FILTER (WHERE e.error IS NOT NULL) AS error_message,
                (
                    SELECT e2.payload_json
                    FROM nfse_execucoes e2
                    WHERE e2.job_id = e.job_id
                    ORDER BY e2.created_at ASC
                    LIMIT 1
                ) AS payload_json
            FROM nfse_execucoes e
            LEFT JOIN nfse_processos p ON p.id = e.processo_id
            WHERE e.job_id = %s
            GROUP BY e.job_id
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None
