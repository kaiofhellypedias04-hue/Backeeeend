from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional, List, Any

from .db import get_conn
from .schemas import ProcessoCreate, ProcessoResponse, StatusEnum


def garantir_schema_nfse_processos():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nfse_processos (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          execution_id UUID,
          cert_alias TEXT NOT NULL,
          login_type TEXT,
          tipo_nota TEXT,
          start_date DATE,
          end_date DATE,
          status TEXT DEFAULT 'queued',
          created_at TIMESTAMP DEFAULT now(),
          started_at TIMESTAMP,
          finished_at TIMESTAMP,
          total_notas INTEGER DEFAULT 0,
          total_xml INTEGER DEFAULT 0,
          total_pdf INTEGER DEFAULT 0,
          total_corretas INTEGER DEFAULT 0,
          total_divergentes INTEGER DEFAULT 0,
          error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_nfse_processos_cert ON nfse_processos(cert_alias);
        CREATE INDEX IF NOT EXISTS idx_nfse_processos_status ON nfse_processos(status);
        CREATE INDEX IF NOT EXISTS idx_nfse_processos_dates ON nfse_processos(start_date, end_date);
        """)


def _uuid_to_str(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _row_to_processo_response(row) -> ProcessoResponse:
    return ProcessoResponse(
        id=_uuid_to_str(row["id"]),
        execution_id=_uuid_to_str(row["execution_id"]),
        cert_alias=row["cert_alias"],
        login_type=row["login_type"],
        tipo_nota=row["tipo_nota"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        total_notas=row["total_notas"] or 0,
        total_xml=row["total_xml"] or 0,
        total_pdf=row["total_pdf"] or 0,
        total_corretas=row["total_corretas"] or 0,
        total_divergentes=row["total_divergentes"] or 0,
        error_message=row["error_message"],
    )


def criar_processo(data: ProcessoCreate) -> str:
    garantir_schema_nfse_processos()
    processo_id = str(uuid.uuid4())
    execution_id = str(data.execution_id) if data.execution_id else str(uuid.uuid4())

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO nfse_processos (id, execution_id, cert_alias, login_type, tipo_nota, start_date, end_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            processo_id,
            execution_id,
            data.cert_alias,
            data.login_type,
            data.tipo_nota,
            data.start_date,
            data.end_date
        ))

    return processo_id


def obter_processo(processo_id: str) -> Optional[ProcessoResponse]:
    garantir_schema_nfse_processos()

    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM nfse_processos WHERE id = %s
        """, (processo_id,)).fetchone()

        if not row:
            return None

        return _row_to_processo_response(row)


def listar_processos(
    cert_alias: Optional[str] = None,
    status: Optional[str] = None,
    execution_id: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    page: int = 1,
    page_size: int = 20
) -> List[ProcessoResponse]:
    garantir_schema_nfse_processos()

    offset = (page - 1) * page_size
    where_clauses = []
    params = []

    if cert_alias:
        where_clauses.append("cert_alias = %s")
        params.append(cert_alias)

    if status:
        where_clauses.append("status = %s")
        params.append(status)

    if execution_id:
        where_clauses.append("execution_id = %s")
        params.append(execution_id)

    if start_date:
        where_clauses.append("start_date >= %s")
        params.append(start_date)

    if end_date:
        where_clauses.append("end_date <= %s")
        params.append(end_date)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    params.extend([page_size, offset])

    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT * FROM nfse_processos
            {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, params).fetchall()

    return [_row_to_processo_response(row) for row in rows]


def atualizar_status_processo(
    processo_id: str,
    status: StatusEnum,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    error_message: Optional[str] = None
):
    garantir_schema_nfse_processos()

    sets = ["status = %s"]
    params = [status.value if hasattr(status, "value") else str(status)]

    if started_at is not None:
        sets.append("started_at = %s")
        params.append(started_at)

    if finished_at is not None:
        sets.append("finished_at = %s")
        params.append(finished_at)

    if error_message is not None:
        sets.append("error_message = %s")
        params.append(error_message)

    params.append(processo_id)

    with get_conn() as conn:
        conn.execute(f"""
            UPDATE nfse_processos
            SET {", ".join(sets)}
            WHERE id = %s
        """, params)


def atualizar_totais_processo(
    processo_id: str,
    total_notas: int,
    total_xml: int = 0,
    total_pdf: int = 0,
    total_corretas: int = 0,
    total_divergentes: int = 0
):
    garantir_schema_nfse_processos()

    with get_conn() as conn:
        conn.execute("""
            UPDATE nfse_processos
            SET total_notas = %s,
                total_xml = %s,
                total_pdf = %s,
                total_corretas = %s,
                total_divergentes = %s
            WHERE id = %s
        """, (
            total_notas,
            total_xml,
            total_pdf,
            total_corretas,
            total_divergentes,
            processo_id
        ))