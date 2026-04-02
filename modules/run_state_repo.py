from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .db import get_conn


def garantir_schema_run_state():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nfse_run_state (
          cert_alias TEXT PRIMARY KEY,
          last_processed_date DATE,
          last_run_at TIMESTAMP,
          status TEXT,
          last_error TEXT
        );
        """)


@dataclass
class RunState:
    cert_alias: str
    last_processed_date: date | None
    last_run_at: datetime | None
    status: str | None
    last_error: str | None


def get_state(cert_alias: str) -> RunState | None:
    garantir_schema_run_state()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT cert_alias, last_processed_date, last_run_at, status, last_error
               FROM nfse_run_state WHERE cert_alias = %s""",
            (cert_alias,),
        ).fetchone()
        if not row:
            return None
        return RunState(
            cert_alias=row["cert_alias"],
            last_processed_date=row.get("last_processed_date"),
            last_run_at=row.get("last_run_at"),
            status=row.get("status"),
            last_error=row.get("last_error"),
        )


def upsert_state(
    cert_alias: str,
    *,
    last_processed_date: date | None = None,
    status: str | None = None,
    last_error: str | None = None,
):
    garantir_schema_run_state()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO nfse_run_state (cert_alias, last_processed_date, last_run_at, status, last_error)
            VALUES (%s, %s, now(), %s, %s)
            ON CONFLICT (cert_alias) DO UPDATE SET
              last_processed_date = COALESCE(EXCLUDED.last_processed_date, nfse_run_state.last_processed_date),
              last_run_at = now(),
              status = COALESCE(EXCLUDED.status, nfse_run_state.status),
              last_error = EXCLUDED.last_error
            """,
            (cert_alias, last_processed_date, status, last_error),
        )
