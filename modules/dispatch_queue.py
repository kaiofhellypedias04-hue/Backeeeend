from __future__ import annotations

import logging
import random
import threading
import time
import zlib
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .db import get_database_url, get_conn
from .execucoes_repo import atualizar_status_execucao
from .processos_repo import atualizar_status_processo, obter_processo
from .runner_processos import ProcessRunConfig, run_with_process
from .schemas import StatusEnum
from .settings import get_settings

logger = logging.getLogger("dispatch")

DISPATCH_STATUS_QUEUED = "queued"
DISPATCH_STATUS_RUNNING = "running"
DISPATCH_STATUS_COMPLETED = "completed"
DISPATCH_STATUS_FAILED = "failed"
DISPATCH_STATUS_CANCELLED = "cancelled"

DISPATCH_LOCK_KEY = 91530051
SCHEDULER_LOCK_NAMESPACE = 91530052
DISPATCH_IDLE_POLL_SECONDS = 5
COOLDOWN_MIN_SECONDS = 180
COOLDOWN_MAX_SECONDS = 300

_dispatcher_thread: threading.Thread | None = None
_dispatcher_guard = threading.Lock()


def garantir_schema_nfse_dispatch_queue() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nfse_dispatch_queue (
              id BIGSERIAL PRIMARY KEY,
              job_id TEXT NOT NULL,
              processo_id UUID NOT NULL,
              cert_alias TEXT NOT NULL,
              payload_json JSONB NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              created_at TIMESTAMP NOT NULL DEFAULT now(),
              started_at TIMESTAMP,
              finished_at TIMESTAMP,
              available_after TIMESTAMP NOT NULL DEFAULT now(),
              last_error TEXT,
              attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_nfse_dispatch_queue_status_available
              ON nfse_dispatch_queue (status, available_after, created_at);
            CREATE INDEX IF NOT EXISTS idx_nfse_dispatch_queue_job
              ON nfse_dispatch_queue (job_id);
            CREATE INDEX IF NOT EXISTS idx_nfse_dispatch_queue_processo
              ON nfse_dispatch_queue (processo_id);
            """
        )


def enqueue_dispatch_item(
    *,
    job_id: str,
    processo_id: str,
    cert_alias: str,
    payload_json: dict[str, Any],
    available_after: datetime | None = None,
) -> int:
    garantir_schema_nfse_dispatch_queue()
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO nfse_dispatch_queue (
              job_id, processo_id, cert_alias, payload_json, status, available_after
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                job_id,
                processo_id,
                cert_alias,
                Jsonb(payload_json),
                DISPATCH_STATUS_QUEUED,
                available_after or datetime.now(),
            ),
        ).fetchone()
    return int(row["id"])


def _dispatch_payload_to_config(item: dict[str, Any]) -> ProcessRunConfig:
    payload = item.get("payload_json") or {}

    def _parse_date(value: Any) -> date | None:
        if not value:
            return None
        if isinstance(value, date):
            return value
        return datetime.fromisoformat(str(value)).date()

    return ProcessRunConfig(
        modo=str(payload.get("modo") or "manual"),
        base_dir=str(payload["base_dir"]),
        certs_json_path=str(payload["certs_json_path"]),
        credentials_json_path=str(payload.get("credentials_json_path") or ""),
        cert_aliases=[str(payload["cert_alias"])],
        start=_parse_date(payload.get("start")),
        end=_parse_date(payload.get("end")),
        headless=bool(payload.get("headless", False)),
        use_chunk_days=bool(payload.get("use_chunk_days", False)),
        chunk_days=int(payload.get("chunk_days") or 30),
        consultar_api=bool(payload.get("consultar_api", True)),
        login_type=str(payload.get("login_type") or "certificado"),
        tipo_nota=str(payload.get("tipo_nota") or "tomados"),
        execution_id=str(item["job_id"]),
        processo_id=str(item["processo_id"]),
    )


def _try_open_lock_connection() -> Optional[psycopg.Connection]:
    settings = get_settings()
    conn = psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
        connect_timeout=settings.db_connect_timeout,
        sslmode=settings.db_sslmode or "require",
    )
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (DISPATCH_LOCK_KEY,)).fetchone()
    if row and row.get("locked"):
        return conn
    conn.close()
    return None


def _next_dispatch_item() -> Optional[dict[str, Any]]:
    garantir_schema_nfse_dispatch_queue()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, job_id, processo_id, cert_alias, payload_json, status, created_at,
                   started_at, finished_at, available_after, last_error, attempts
            FROM nfse_dispatch_queue
            WHERE status = %s
              AND available_after <= now()
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (DISPATCH_STATUS_QUEUED,),
        ).fetchone()
    return dict(row) if row else None


def _mark_dispatch_running(queue_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE nfse_dispatch_queue
            SET status = %s,
                started_at = COALESCE(started_at, now()),
                last_error = NULL,
                attempts = attempts + 1
            WHERE id = %s
            """,
            (DISPATCH_STATUS_RUNNING, queue_id),
        )


def _mark_dispatch_terminal(queue_id: int, status: str, last_error: str | None = None) -> datetime:
    cooldown_until = datetime.now() + timedelta(seconds=random.randint(COOLDOWN_MIN_SECONDS, COOLDOWN_MAX_SECONDS))
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE nfse_dispatch_queue
            SET status = %s,
                finished_at = now(),
                available_after = %s,
                last_error = %s
            WHERE id = %s
            """,
            (status, cooldown_until, last_error, queue_id),
        )
    return cooldown_until


def _current_cooldown_until() -> Optional[datetime]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT available_after
            FROM nfse_dispatch_queue
            WHERE status IN (%s, %s)
              AND finished_at IS NOT NULL
            ORDER BY finished_at DESC, id DESC
            LIMIT 1
            """,
            (DISPATCH_STATUS_COMPLETED, DISPATCH_STATUS_FAILED),
        ).fetchone()
    return row.get("available_after") if row else None


def reconcile_orphaned_dispatch_items() -> int:
    garantir_schema_nfse_dispatch_queue()
    reconciled = 0
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, processo_id, cert_alias
            FROM nfse_dispatch_queue
            WHERE status = %s
            ORDER BY started_at ASC NULLS LAST, created_at ASC
            """,
            (DISPATCH_STATUS_RUNNING,),
        ).fetchall()
        for row in rows:
            error_message = "Execucao interrompida por restart/deploy; item running reconciliado pelo dispatcher."
            cooldown_until = datetime.now() + timedelta(seconds=random.randint(COOLDOWN_MIN_SECONDS, COOLDOWN_MAX_SECONDS))
            conn.execute(
                """
                UPDATE nfse_dispatch_queue
                SET status = %s,
                    finished_at = now(),
                    available_after = %s,
                    last_error = %s
                WHERE id = %s
                """,
                (DISPATCH_STATUS_FAILED, cooldown_until, error_message, row["id"]),
            )
            atualizar_status_processo(
                str(row["processo_id"]),
                StatusEnum.failed,
                finished_at=datetime.now(),
                error_message=error_message,
            )
            atualizar_status_execucao(
                str(row["processo_id"]),
                "failed",
                finished_at=datetime.now(),
                error=error_message,
                traceback="dispatch_orphan_reconciled",
            )
            reconciled += 1
    return reconciled


def _sleep_until(target: datetime | None) -> None:
    if not target:
        time.sleep(DISPATCH_IDLE_POLL_SECONDS)
        return
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(DISPATCH_IDLE_POLL_SECONDS, remaining))


def _dispatcher_leader_loop() -> None:
    reconciled = reconcile_orphaned_dispatch_items()
    if reconciled:
        logger.warning("[Dispatch] %s item(ns) running reconciliado(s) apos restart/deploy.", reconciled)

    while True:
        cooldown_until = _current_cooldown_until()
        if cooldown_until and cooldown_until > datetime.now():
            _sleep_until(cooldown_until)
            continue

        item = _next_dispatch_item()
        if not item:
            time.sleep(DISPATCH_IDLE_POLL_SECONDS)
            continue

        queue_id = int(item["id"])
        _mark_dispatch_running(queue_id)
        logger.info(
            "[Dispatch] Iniciando item de fila id=%s processo=%s alias=%s",
            queue_id,
            item["processo_id"],
            item["cert_alias"],
        )
        try:
            cfg = _dispatch_payload_to_config(item)
            run_with_process(cfg)
            cooldown_until = _mark_dispatch_terminal(queue_id, DISPATCH_STATUS_COMPLETED)
            logger.info(
                "[Dispatch] Processo %s concluido. Cooldown global ate %s.",
                item["processo_id"],
                cooldown_until.isoformat(),
            )
        except Exception as exc:
            error_message = str(exc)
            proc = obter_processo(str(item["processo_id"]))
            if proc and proc.status not in {StatusEnum.completed, StatusEnum.failed}:
                atualizar_status_processo(
                    str(item["processo_id"]),
                    StatusEnum.failed,
                    finished_at=datetime.now(),
                    error_message=error_message,
                )
                atualizar_status_execucao(
                    str(item["processo_id"]),
                    "failed",
                    finished_at=datetime.now(),
                    error=error_message,
                    traceback="dispatch_failure_before_runner_completion",
                )
            cooldown_until = _mark_dispatch_terminal(queue_id, DISPATCH_STATUS_FAILED, error_message)
            logger.exception(
                "[Dispatch] Processo %s falhou e entrou em cooldown ate %s.",
                item["processo_id"],
                cooldown_until.isoformat(),
            )
        _sleep_until(cooldown_until)


def _dispatcher_loop() -> None:
    while True:
        lock_conn = None
        try:
            lock_conn = _try_open_lock_connection()
            if lock_conn is None:
                time.sleep(DISPATCH_IDLE_POLL_SECONDS)
                continue
            logger.info("[Dispatch] Dispatcher lider ativo com advisory lock global.")
            _dispatcher_leader_loop()
        except Exception:
            logger.exception("[Dispatch] Falha no loop do dispatcher; tentativa de recuperacao em andamento.")
            time.sleep(DISPATCH_IDLE_POLL_SECONDS)
        finally:
            if lock_conn is not None:
                try:
                    lock_conn.execute("SELECT pg_advisory_unlock(%s)", (DISPATCH_LOCK_KEY,))
                except Exception:
                    pass
                lock_conn.close()


def start_dispatcher() -> None:
    global _dispatcher_thread
    with _dispatcher_guard:
        if _dispatcher_thread and _dispatcher_thread.is_alive():
            return
        garantir_schema_nfse_dispatch_queue()
        _dispatcher_thread = threading.Thread(
            target=_dispatcher_loop,
            daemon=True,
            name="nfse-dispatcher",
        )
        _dispatcher_thread.start()


def _scheduler_lock_key(job_id: str, slot_key: str) -> int:
    raw = f"{job_id}:{slot_key}".encode("utf-8", errors="ignore")
    return zlib.crc32(raw) ^ SCHEDULER_LOCK_NAMESPACE


@contextmanager
def scheduler_dispatch_guard(job_id: str, slot_key: str) -> Iterator[bool]:
    settings = get_settings()
    conn = psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
        connect_timeout=settings.db_connect_timeout,
        sslmode=settings.db_sslmode or "require",
    )
    lock_key = _scheduler_lock_key(job_id, slot_key)
    try:
        row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (lock_key,)).fetchone()
        locked = bool(row and row.get("locked"))
        yield locked
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        except Exception:
            pass
        conn.close()
