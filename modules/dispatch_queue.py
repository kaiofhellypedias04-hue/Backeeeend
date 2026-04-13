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
from .processos_repo import atualizar_status_processo, obter_processo, obter_status_processo
from .runner_processos import ProcessCancelledError, ProcessRunConfig, run_with_process
from .schemas import StatusEnum, normalize_login_type, normalize_tipo_nota
from .settings import get_settings
from .timezone_utils import ensure_utc_datetime, now_utc

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
_dispatcher_state_guard = threading.Lock()
_dispatcher_state: dict[str, Any] = {
    "thread_name": "nfse-dispatcher",
    "thread_alive": False,
    "started_at": None,
    "last_loop_at": None,
    "last_poll_at": None,
    "last_lock_attempt_at": None,
    "leader_acquired_at": None,
    "last_item_id": None,
    "last_processo_id": None,
    "last_error": None,
    "last_event": "idle",
    "is_leader": False,
}


def _update_dispatcher_state(**kwargs: Any) -> None:
    with _dispatcher_state_guard:
        _dispatcher_state.update(kwargs)


def get_dispatcher_debug_snapshot() -> dict[str, Any]:
    garantir_schema_nfse_dispatch_queue()
    with _dispatcher_state_guard:
        snapshot = dict(_dispatcher_state)
        thread = _dispatcher_thread
        snapshot["thread_alive"] = bool(thread and thread.is_alive())
    with get_conn() as conn:
        counts_row = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = %s) AS queued,
              COUNT(*) FILTER (WHERE status = %s) AS running,
              COUNT(*) FILTER (WHERE status = %s) AS completed,
              COUNT(*) FILTER (WHERE status = %s) AS failed,
              COUNT(*) FILTER (WHERE status = %s) AS cancelled
            FROM nfse_dispatch_queue
            """
            ,
            (
                DISPATCH_STATUS_QUEUED,
                DISPATCH_STATUS_RUNNING,
                DISPATCH_STATUS_COMPLETED,
                DISPATCH_STATUS_FAILED,
                DISPATCH_STATUS_CANCELLED,
            ),
        ).fetchone()
    snapshot["queue_counts"] = dict(counts_row) if counts_row else {}
    return snapshot


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
                available_after or now_utc(),
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
        lookback_days=int(payload.get("lookback_days") or 30),
        use_chunk_days=bool(payload.get("use_chunk_days", False)),
        chunk_days=int(payload.get("chunk_days") or 30),
        consultar_api=bool(payload.get("consultar_api", True)),
        login_type=normalize_login_type(payload.get("login_type")),
        tipo_nota=normalize_tipo_nota(payload.get("tipo_nota")),
        execution_id=str(item["job_id"]),
        processo_id=str(item["processo_id"]),
    )


def _try_open_lock_connection() -> Optional[psycopg.Connection]:
    settings = get_settings()
    _update_dispatcher_state(
        last_lock_attempt_at=now_utc(),
        last_loop_at=now_utc(),
        last_event="trying_advisory_lock",
    )
    logger.info("[Dispatch] Tentando adquirir advisory lock global do dispatcher.")
    conn = psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
        connect_timeout=settings.db_connect_timeout,
        sslmode=settings.db_sslmode or "require",
    )
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (DISPATCH_LOCK_KEY,)).fetchone()
    if row and row.get("locked"):
        _update_dispatcher_state(
            is_leader=True,
            leader_acquired_at=now_utc(),
            last_event="leader_active",
            last_error=None,
        )
        logger.info("[Dispatch] Advisory lock adquirido; esta instancia virou lider.")
        return conn
    _update_dispatcher_state(is_leader=False, last_event="advisory_lock_not_acquired")
    logger.info("[Dispatch] Advisory lock nao adquirido; outra instancia permanece lider.")
    conn.close()
    return None


def _next_dispatch_item() -> Optional[dict[str, Any]]:
    garantir_schema_nfse_dispatch_queue()
    _update_dispatcher_state(last_poll_at=now_utc(), last_loop_at=now_utc(), last_event="polling_queue")
    logger.info("[Dispatch] Polling da fila iniciado.")
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
    if row:
        logger.info(
            "[Dispatch] Item elegivel encontrado na fila id=%s processo=%s alias=%s.",
            row["id"],
            row["processo_id"],
            row["cert_alias"],
        )
        _update_dispatcher_state(
            last_item_id=row["id"],
            last_processo_id=str(row["processo_id"]),
            last_event="item_found",
        )
    else:
        logger.info("[Dispatch] Fila vazia no polling atual.")
        _update_dispatcher_state(last_event="queue_empty")
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
    _update_dispatcher_state(last_item_id=queue_id, last_event="item_marked_running")
    logger.info("[Dispatch] Item id=%s marcado como running.", queue_id)


def _mark_dispatch_terminal(
    queue_id: int,
    status: str,
    last_error: str | None = None,
    *,
    apply_cooldown: bool = True,
) -> datetime:
    cooldown_until = (
        now_utc() + timedelta(seconds=random.randint(COOLDOWN_MIN_SECONDS, COOLDOWN_MAX_SECONDS))
        if apply_cooldown
        else now_utc()
    )
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
    _update_dispatcher_state(last_item_id=queue_id, last_event=f"item_terminal:{status}")
    return cooldown_until


def cancel_dispatch_items_for_process(
    processo_id: str,
    *,
    reason: str = "Processo cancelado manualmente.",
) -> dict[str, int]:
    garantir_schema_nfse_dispatch_queue()
    with get_conn() as conn:
        before_row = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = %s) AS queued,
              COUNT(*) FILTER (WHERE status = %s) AS running
            FROM nfse_dispatch_queue
            WHERE processo_id = %s
              AND status IN (%s, %s)
            """,
            (
                DISPATCH_STATUS_QUEUED,
                DISPATCH_STATUS_RUNNING,
                processo_id,
                DISPATCH_STATUS_QUEUED,
                DISPATCH_STATUS_RUNNING,
            ),
        ).fetchone()
        conn.execute(
            """
            UPDATE nfse_dispatch_queue
            SET status = CASE
                    WHEN status = %s THEN %s
                    ELSE status
                END,
                finished_at = CASE
                    WHEN status = %s THEN now()
                    ELSE finished_at
                END,
                available_after = CASE
                    WHEN status = %s THEN now()
                    ELSE available_after
                END,
                last_error = %s
            WHERE processo_id = %s
              AND status IN (%s, %s)
            """,
            (
                DISPATCH_STATUS_QUEUED,
                DISPATCH_STATUS_CANCELLED,
                DISPATCH_STATUS_QUEUED,
                DISPATCH_STATUS_QUEUED,
                reason,
                processo_id,
                DISPATCH_STATUS_QUEUED,
                DISPATCH_STATUS_RUNNING,
            ),
        )
    result = {
        "queued_cancelled": int((before_row or {}).get("queued") or 0),
        "running_signalled": int((before_row or {}).get("running") or 0),
    }
    logger.warning(
        "[Dispatch] Cancelamento solicitado para processo=%s queued_cancelled=%s running_signalled=%s.",
        processo_id,
        result["queued_cancelled"],
        result["running_signalled"],
    )
    return result


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
    return ensure_utc_datetime(row.get("available_after")) if row else None


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
            processo_id = str(row["processo_id"])
            processo_status = obter_status_processo(processo_id)
            if processo_status == StatusEnum.cancelled.value:
                error_message = "Processo cancelado permaneceu como running na fila apos restart; item reconciliado como cancelled."
                terminal_status = DISPATCH_STATUS_CANCELLED
                cooldown_until = now_utc()
                atualizar_status_execucao(
                    processo_id,
                    "cancelled",
                    finished_at=now_utc(),
                    error=error_message,
                    traceback="dispatch_orphan_cancelled_reconciled",
                )
            else:
                error_message = "Execucao interrompida por restart/deploy; item running reconciliado pelo dispatcher."
                terminal_status = DISPATCH_STATUS_FAILED
                cooldown_until = now_utc() + timedelta(seconds=random.randint(COOLDOWN_MIN_SECONDS, COOLDOWN_MAX_SECONDS))
                atualizar_status_processo(
                    processo_id,
                    StatusEnum.failed,
                    finished_at=now_utc(),
                    error_message=error_message,
                )
                atualizar_status_execucao(
                    processo_id,
                    "failed",
                    finished_at=now_utc(),
                    error=error_message,
                    traceback="dispatch_orphan_reconciled",
                )
            conn.execute(
                """
                UPDATE nfse_dispatch_queue
                SET status = %s,
                    finished_at = now(),
                    available_after = %s,
                    last_error = %s
                WHERE id = %s
                """,
                (terminal_status, cooldown_until, error_message, row["id"]),
            )
            reconciled += 1
    return reconciled


def _sleep_until(target: datetime | None) -> None:
    if not target:
        _update_dispatcher_state(last_event="idle_sleep", last_loop_at=now_utc())
        time.sleep(DISPATCH_IDLE_POLL_SECONDS)
        return
    while True:
        remaining = (target - now_utc()).total_seconds()
        if remaining <= 0:
            return
        _update_dispatcher_state(last_event="cooldown_wait", last_loop_at=now_utc())
        time.sleep(min(DISPATCH_IDLE_POLL_SECONDS, remaining))


def _dispatcher_leader_loop() -> None:
    logger.info("[Dispatch] Loop principal do dispatcher lider iniciado.")
    _update_dispatcher_state(last_event="leader_loop_started", last_loop_at=now_utc())
    reconciled = reconcile_orphaned_dispatch_items()
    if reconciled:
        logger.warning("[Dispatch] %s item(ns) running reconciliado(s) apos restart/deploy.", reconciled)

    while True:
        cooldown_until = _current_cooldown_until()
        if cooldown_until and cooldown_until > now_utc():
            logger.info("[Dispatch] Cooldown global ativo ate %s.", cooldown_until.isoformat())
            _update_dispatcher_state(last_event="cooldown_active", last_loop_at=now_utc())
            _sleep_until(cooldown_until)
            continue

        item = _next_dispatch_item()
        if not item:
            time.sleep(DISPATCH_IDLE_POLL_SECONDS)
            continue

        queue_id = int(item["id"])
        processo_status = obter_status_processo(str(item["processo_id"]))
        if processo_status == StatusEnum.cancelled.value:
            message = "Item cancelado manualmente antes do inicio da execucao."
            logger.warning(
                "[Dispatch] Processo cancelado detectado antes do runner. queue_id=%s processo=%s.",
                queue_id,
                item["processo_id"],
            )
            atualizar_status_execucao(
                str(item["processo_id"]),
                "cancelled",
                finished_at=now_utc(),
                error=message,
                traceback="dispatch_cancelled_before_start",
            )
            _mark_dispatch_terminal(
                queue_id,
                DISPATCH_STATUS_CANCELLED,
                message,
                apply_cooldown=False,
            )
            logger.warning(
                "[Dispatch] Item descartado por cancelamento. id=%s processo=%s.",
                queue_id,
                item["processo_id"],
            )
            continue
        _mark_dispatch_running(queue_id)
        logger.info(
            "[Dispatch] Iniciando item de fila id=%s processo=%s alias=%s",
            queue_id,
            item["processo_id"],
            item["cert_alias"],
        )
        try:
            cfg = _dispatch_payload_to_config(item)
            logger.info("[Dispatch] Processo %s enviado ao runner.", item["processo_id"])
            run_with_process(cfg)
            cooldown_until = _mark_dispatch_terminal(queue_id, DISPATCH_STATUS_COMPLETED)
            logger.info(
                "[Dispatch] Item concluido. processo=%s cooldown_ate=%s.",
                item["processo_id"],
                cooldown_until.isoformat(),
            )
        except ProcessCancelledError as exc:
            message = str(exc)
            logger.warning("[Dispatch] Processo cancelado detectado durante execucao. processo=%s.", item["processo_id"])
            atualizar_status_processo(
                str(item["processo_id"]),
                StatusEnum.cancelled,
                finished_at=now_utc(),
                error_message=message,
            )
            atualizar_status_execucao(
                str(item["processo_id"]),
                "cancelled",
                finished_at=now_utc(),
                error=message,
                traceback="dispatch_cancelled_during_run",
            )
            cooldown_until = _mark_dispatch_terminal(
                queue_id,
                DISPATCH_STATUS_CANCELLED,
                message,
                apply_cooldown=False,
            )
            logger.warning(
                "[Dispatch] Item concluido como cancelled sem cooldown. processo=%s.",
                item["processo_id"],
            )
        except Exception as exc:
            error_message = str(exc)
            proc = obter_processo(str(item["processo_id"]))
            if proc and proc.status not in {StatusEnum.completed, StatusEnum.failed}:
                atualizar_status_processo(
                    str(item["processo_id"]),
                    StatusEnum.failed,
                    finished_at=now_utc(),
                    error_message=error_message,
                )
                atualizar_status_execucao(
                    str(item["processo_id"]),
                    "failed",
                    finished_at=now_utc(),
                    error=error_message,
                    traceback="dispatch_failure_before_runner_completion",
                )
            cooldown_until = _mark_dispatch_terminal(queue_id, DISPATCH_STATUS_FAILED, error_message)
            logger.exception(
                "[Dispatch] Item falhou. processo=%s cooldown_ate=%s.",
                item["processo_id"],
                cooldown_until.isoformat(),
            )
        _sleep_until(cooldown_until)


def _dispatcher_loop() -> None:
    while True:
        lock_conn = None
        try:
            _update_dispatcher_state(thread_alive=True, last_loop_at=now_utc(), last_event="dispatcher_loop_started")
            lock_conn = _try_open_lock_connection()
            if lock_conn is None:
                time.sleep(DISPATCH_IDLE_POLL_SECONDS)
                continue
            logger.info("[Dispatch] Dispatcher lider ativo com advisory lock global.")
            _dispatcher_leader_loop()
        except Exception as exc:
            _update_dispatcher_state(last_error=str(exc), last_event="loop_exception", is_leader=False)
            logger.exception("[Dispatch] Excecao no loop do dispatcher; tentativa de recuperacao em andamento.")
            time.sleep(DISPATCH_IDLE_POLL_SECONDS)
        finally:
            if lock_conn is not None:
                try:
                    lock_conn.execute("SELECT pg_advisory_unlock(%s)", (DISPATCH_LOCK_KEY,))
                except Exception:
                    pass
                lock_conn.close()
            _update_dispatcher_state(is_leader=False, last_loop_at=now_utc())


def start_dispatcher() -> None:
    global _dispatcher_thread
    with _dispatcher_guard:
        logger.info("[Dispatch] start_dispatcher chamado.")
        if _dispatcher_thread and _dispatcher_thread.is_alive():
            logger.info("[Dispatch] Thread do dispatcher ja existente e alive=%s.", _dispatcher_thread.is_alive())
            _update_dispatcher_state(thread_alive=True, last_event="already_running", last_loop_at=now_utc())
            return
        garantir_schema_nfse_dispatch_queue()
        _dispatcher_thread = threading.Thread(
            target=_dispatcher_loop,
            daemon=True,
            name="nfse-dispatcher",
        )
        _dispatcher_thread.start()
        _update_dispatcher_state(
            thread_name=_dispatcher_thread.name,
            thread_alive=_dispatcher_thread.is_alive(),
            started_at=now_utc(),
            last_loop_at=now_utc(),
            last_event="thread_started",
            last_error=None,
        )
        logger.info(
            "[Dispatch] Thread criada name=%s alive=%s ident=%s.",
            _dispatcher_thread.name,
            _dispatcher_thread.is_alive(),
            _dispatcher_thread.ident,
        )


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
