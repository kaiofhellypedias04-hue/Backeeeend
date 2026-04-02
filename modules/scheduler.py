"""
Scheduler com persistência em banco de dados.

Os jobs agendados são salvos na tabela nfse_agendamentos para sobreviver
a reinicializações da API. Na inicialização (startup) a API restaura
automaticamente todos os jobs ativos.
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, Any, List, Optional

from .db import get_conn

# Jobs em memória: { job_id: { "ativo": bool, thread, ... } }
_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def garantir_schema_agendamentos():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nfse_agendamentos (
            job_id TEXT PRIMARY KEY,
            descricao TEXT,
            intervalo_segundos INTEGER NOT NULL DEFAULT 86400,
            ativo BOOLEAN NOT NULL DEFAULT TRUE,
            payload_json JSONB,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            ultima_execucao TIMESTAMP,
            proxima_execucao TIMESTAMP,
            total_execucoes INTEGER NOT NULL DEFAULT 0,
            ultimo_erro TEXT
        );
        """)


def _atualizar_execucao_db(job_id: str, ultima: datetime, proxima: datetime, erro: Optional[str] = None):
    try:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE nfse_agendamentos
                SET ultima_execucao = %s,
                    proxima_execucao = %s,
                    total_execucoes = total_execucoes + 1,
                    ultimo_erro = %s
                WHERE job_id = %s
                """,
                (ultima, proxima, erro, job_id),
            )
    except Exception as e:
        print(f"[SCHEDULER] Erro ao atualizar execução no banco: {e}")


def _marcar_inativo_db(job_id: str):
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE nfse_agendamentos SET ativo = FALSE WHERE job_id = %s",
                (job_id,),
            )
    except Exception as e:
        print(f"[SCHEDULER] Erro ao marcar job inativo no banco: {e}")


def iniciar_agendamento(
    job_id: str,
    func: Callable,
    intervalo_segundos: int = 86400,
    descricao: str = "",
    payload: Optional[dict] = None,
) -> str:
    garantir_schema_agendamentos()

    agora = datetime.now()
    proxima = agora  # primeira execução imediata

    # Persistir no banco
    try:
        from psycopg.types.json import Jsonb
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO nfse_agendamentos
                    (job_id, descricao, intervalo_segundos, ativo, payload_json,
                     created_at, proxima_execucao)
                VALUES (%s, %s, %s, TRUE, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET
                    ativo = TRUE,
                    descricao = EXCLUDED.descricao,
                    intervalo_segundos = EXCLUDED.intervalo_segundos,
                    payload_json = EXCLUDED.payload_json,
                    proxima_execucao = EXCLUDED.proxima_execucao
                """,
                (job_id, descricao, intervalo_segundos, Jsonb(payload or {}), agora, proxima),
            )
    except Exception as e:
        print(f"[SCHEDULER] Aviso: não foi possível persistir agendamento no banco: {e}")

    with _lock:
        _jobs[job_id] = {
            "ativo": True,
            "intervalo_segundos": intervalo_segundos,
            "descricao": descricao,
            "created_at": agora.isoformat(),
            "ultima_execucao": None,
            "proxima_execucao": proxima.isoformat(),
            "total_execucoes": 0,
        }

    def loop():
        while _jobs.get(job_id, {}).get("ativo"):
            erro_str = None
            agora_exec = datetime.now()
            try:
                with _lock:
                    if job_id in _jobs:
                        _jobs[job_id]["ultima_execucao"] = agora_exec.isoformat()

                func()

                with _lock:
                    if job_id in _jobs:
                        _jobs[job_id]["total_execucoes"] = _jobs[job_id].get("total_execucoes", 0) + 1

            except Exception as e:
                erro_str = str(e)
                print(f"[SCHEDULER ERRO] job={job_id} erro={e}")

            proxima_dt = datetime.now() + timedelta(seconds=intervalo_segundos)
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["proxima_execucao"] = proxima_dt.isoformat()

            _atualizar_execucao_db(job_id, agora_exec, proxima_dt, erro_str)

            # Dormir em fatias para responder rapidamente ao parar
            restante = intervalo_segundos
            while restante > 0 and _jobs.get(job_id, {}).get("ativo"):
                time.sleep(min(30, restante))
                restante -= 30

    t = threading.Thread(target=loop, daemon=True, name=f"scheduler-{job_id}")
    t.start()

    with _lock:
        _jobs[job_id]["_thread"] = t

    return job_id


def parar_agendamento(job_id: str):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["ativo"] = False
    _marcar_inativo_db(job_id)


def listar_agendamentos() -> List[Dict[str, Any]]:
    """Retorna estado dos jobs ativos em memória enriquecido com dados do banco."""
    garantir_schema_agendamentos()
    resultado = []

    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT job_id, descricao, intervalo_segundos, ativo,
                       payload_json, created_at, ultima_execucao,
                       proxima_execucao, total_execucoes, ultimo_erro
                FROM nfse_agendamentos
                WHERE ativo = TRUE
                ORDER BY created_at DESC
                """
            ).fetchall()

        for row in rows:
            d = dict(row)
            job_id = d["job_id"]
            em_memoria = job_id in _jobs and _jobs[job_id].get("ativo", False)
            d["running"] = em_memoria
            resultado.append(d)
    except Exception:
        # Fallback para memória se banco não estiver disponível
        with _lock:
            for job_id, info in _jobs.items():
                if info.get("ativo"):
                    resultado.append({
                        "job_id": job_id,
                        "running": True,
                        "intervalo_segundos": info.get("intervalo_segundos"),
                        "descricao": info.get("descricao", ""),
                        "ultima_execucao": info.get("ultima_execucao"),
                        "proxima_execucao": info.get("proxima_execucao"),
                        "total_execucoes": info.get("total_execucoes", 0),
                    })

    return resultado


def restaurar_agendamentos_do_banco(factory: Callable[[dict], Callable]) -> int:
    """
    Chamado no startup da API para reativar jobs que estavam ativos
    antes da última reinicialização.

    factory: função que recebe o payload_json e retorna a função a executar.
    Retorna o número de jobs restaurados.
    """
    garantir_schema_agendamentos()
    restaurados = 0

    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT job_id, descricao, intervalo_segundos, payload_json
                FROM nfse_agendamentos
                WHERE ativo = TRUE
                ORDER BY created_at ASC
                """
            ).fetchall()

        for row in rows:
            d = dict(row)
            job_id = d["job_id"]

            # Não recriar jobs que já estão rodando em memória
            if job_id in _jobs and _jobs[job_id].get("ativo"):
                continue

            try:
                func = factory(d.get("payload_json") or {})
                if func is None:
                    continue
                iniciar_agendamento(
                    job_id=job_id,
                    func=func,
                    intervalo_segundos=d.get("intervalo_segundos", 86400),
                    descricao=d.get("descricao", ""),
                    payload=d.get("payload_json") or {},
                )
                restaurados += 1
                print(f"[SCHEDULER] Job restaurado: {job_id}")
            except Exception as e:
                print(f"[SCHEDULER] Erro ao restaurar job {job_id}: {e}")

    except Exception as e:
        print(f"[SCHEDULER] Erro ao restaurar agendamentos do banco: {e}")

    return restaurados
