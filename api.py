"""
API Auditoria NFS-e — v2.2.0

Novidades em relação à v2.1.0:
  - base_dir removido do ExecRequest — o servidor define o diretório de saída
    automaticamente em DATA_DIR/{alias} (configurável via env DATA_DIR)
  - Nova rota GET /processos/{id}/download-zip — empacota todos os arquivos
    (PDFs, XMLs, planilha) de um processo em um .zip e retorna para download
  - Nova rota GET /processos/{id}/relatorio-csv — exporta o relatório completo
    do processo em CSV com todos os campos de auditoria, pronto para Excel
  - CORS aberto para qualquer origem por padrão (ajuste CORS_ORIGINS no .env
    para restringir em produção)
"""

import io
import logging
import os
import re
import uuid
import zipfile
import csv
from pathlib import Path
from datetime import date, datetime, timedelta
from threading import Thread
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from modules.processos_repo import criar_processo, obter_processo, listar_processos, atualizar_status_processo
from modules.execucoes_repo import (
    criar_execucao,
    obter_execucao,
    atualizar_status_execucao,
    listar_execucoes,
    garantir_schema_nfse_execucoes,
)
from modules.arquivos_repo import (
    listar_arquivos_processo,
    obter_arquivo_processo,
    garantir_schema_nfse_processo_arquivos,
)
from modules.notas_repo import (
    garantir_schema_nfse_notas,
    listar_notas_por_processo,
    obter_resumo_processo,
    listar_notas_agrupadas,
    listar_empresas_e_contadores_fila,
    atualizar_nota_campos_editaveis,
    listar_regras_atribuicao,
    criar_regra_atribuicao,
    atualizar_regra_atribuicao,
    excluir_regra_atribuicao,
    reaplicar_regras_atribuicao,
    localizar_documentos_nota,
)
from modules.runner_processos import run_with_process, ProcessRunConfig, RunConfig
from modules.certificados_repo import garantir_schema_nfse_certificados, migrar_certificados_legados
from modules.certificados_secret_repo import garantir_schema_nfse_certificados_segredos
from modules.cert_storage import certificate_display_name
from modules.dispatch_queue import (
    cancel_dispatch_items_for_process,
    get_dispatcher_debug_snapshot,
    enqueue_dispatch_item,
    garantir_schema_nfse_dispatch_queue,
    scheduler_dispatch_guard,
    start_dispatcher,
)
from modules.storage import is_s3_configured, generate_presigned_download_url, limpar_arquivos_antigos_minio
from modules.schemas import (
    StatusEnum, LoginTypeEnum, TipoNotaEnum, Pagination,
    ProcessoResponse, ArquivoResponse, NotaReportFilters,
    NotaReportRow, SummaryResponse, ProcessoCreate, NotaDocumentosResponse, NotaDocumentoItem,
    RegraAtribuicaoCreate, RegraAtribuicaoUpdate, RegraAtribuicaoResponse,
    normalize_login_type,
    normalize_tipo_nota,
)
from modules.reports import gerar_relatorio_processo
from modules.db import get_conn, ensure_database_extensions
from modules.config_loader import carregar_certificados, carregar_credenciais
from modules.export_utils import serialize_export_value
from modules.scheduler import (
    iniciar_agendamento, parar_agendamento, listar_agendamentos,
    restaurar_agendamentos_do_banco,
)
from modules.timezone_utils import now_sao_paulo, now_utc, today_sao_paulo
from modules.cert_manager import (
    adicionar_certificado, editar_certificado, excluir_certificado,
    redefinir_senha_certificado,
    adicionar_credencial, editar_credencial, excluir_credencial,
    redefinir_senha_credencial,
    validar_cpf_cnpj,
)
from modules.settings import BASE_DATA_DIR, CERTS_DIR, OUTPUT_DIR, TEMP_DIR, get_settings


# ─── App e CORS ───────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
settings = get_settings()
settings.ensure_runtime_dirs()

app = FastAPI(title=settings.app_name, version=settings.app_version)

_allowed_origins = settings.cors_origins if settings.cors_origins else ([] if settings.app_env == "production" else ["*"])
_allow_credentials = False if not _allowed_origins or "*" in _allowed_origins else True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=None,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Schemas de request ───────────────────────────────────────────────────────

class ExecRequest(BaseModel):
    cert_aliases: List[str] = Field(..., description="Lista de aliases dos certificados ou credenciais")
    start: date
    end: date
    headless: bool = True
    lookback_days: int = Field(
        30,
        ge=1,
        description="Janela total em dias usada no modo automatico; nao afeta chunk_days",
    )
    use_chunk_days: bool = False
    chunk_days: int = 30
    consultar_api: bool = True
    login_type: LoginTypeEnum = LoginTypeEnum.certificado
    tipo_nota: TipoNotaEnum = TipoNotaEnum.tomados
    hora_execucao: str = Field(
        "06:00",
        description="Horário diário de execução no formato HH:MM (usado apenas no modo agendado)",
        pattern=r"^\d{2}:\d{2}$",
    )


def _validar_intervalo_execucao(start: date, end: date) -> None:
    if start > end:
        raise HTTPException(status_code=400, detail="'start' não pode ser maior que 'end'")
    if (end - start).days > 31:
        raise HTTPException(status_code=400, detail="DATA SUPERIOR A 31 DIAS")


class CredencialCreate(BaseModel):
    alias: str
    cpf_cnpj: str
    password: str


class CredencialEdit(BaseModel):
    novo_alias: Optional[str] = None
    cpf_cnpj: Optional[str] = None


class CertificadoEdit(BaseModel):
    novo_alias: Optional[str] = None
    client_name: Optional[str] = None


class SenhaUpdate(BaseModel):
    password: str


class NotaEditRequest(BaseModel):
    valor_liquido_correto: Optional[float] = None
    alertas_fiscais: Optional[str] = None
    observacao_interna: Optional[str] = None
    status_fila_manual: Optional[str] = None
    prioridade_manual: Optional[str] = None
    responsavel: Optional[str] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

projeto_root = settings.project_root

# Diretório base de saída no servidor — configurável via env DATA_DIR
# Padrão: pasta "saida" dentro do projeto
def _get_data_dir(cert_alias: str = "") -> str:
    base = settings.output_dir
    if cert_alias:
        safe = re.sub(r"[^\w\-. ]", "_", cert_alias).strip()
        return str(base / safe)
    return str(base)


def _build_run_config(req: ExecRequest, cert_alias: str) -> RunConfig:
    logger.info(
        "Montando RunConfig",
        extra={
            "cert_alias": cert_alias,
            "start": req.start.isoformat() if req.start else None,
            "end": req.end.isoformat() if req.end else None,
            "lookback_days": req.lookback_days,
            "use_chunk_days": req.use_chunk_days,
            "chunk_days": req.chunk_days,
            "login_type": str(req.login_type),
            "tipo_nota": normalize_tipo_nota(req.tipo_nota),
        },
    )
    return RunConfig(
        modo="manual",
        base_dir=_get_data_dir(cert_alias),
        certs_json_path=str(settings.certs_json_path),
        credentials_json_path=str(settings.credentials_json_path),
        cert_aliases=[cert_alias],
        start=req.start,
        end=req.end,
        headless=req.headless,
        lookback_days=req.lookback_days,
        use_chunk_days=req.use_chunk_days,
        chunk_days=req.chunk_days,
        consultar_api=req.consultar_api,
        login_type=normalize_login_type(req.login_type),
        tipo_nota=normalize_tipo_nota(req.tipo_nota),
    )


def _build_automatic_run_config(req: ExecRequest, cert_alias: str, start: date, end: date) -> RunConfig:
    return RunConfig(
        modo="manual",
        base_dir=_get_data_dir(cert_alias),
        certs_json_path=str(settings.certs_json_path),
        credentials_json_path=str(settings.credentials_json_path),
        cert_aliases=[cert_alias],
        start=start,
        end=end,
        headless=req.headless,
        lookback_days=req.lookback_days,
        use_chunk_days=req.use_chunk_days,
        chunk_days=req.chunk_days,
        consultar_api=req.consultar_api,
        login_type=normalize_login_type(req.login_type),
        tipo_nota=normalize_tipo_nota(req.tipo_nota),
    )


def _queue_payload_from_config(cfg: RunConfig, cert_alias: str) -> dict:
    return {
        "modo": cfg.modo,
        "base_dir": cfg.base_dir,
        "certs_json_path": cfg.certs_json_path,
        "credentials_json_path": cfg.credentials_json_path,
        "cert_alias": cert_alias,
        "start": cfg.start.isoformat() if cfg.start else None,
        "end": cfg.end.isoformat() if cfg.end else None,
        "headless": cfg.headless,
        "lookback_days": getattr(cfg, "lookback_days", 30),
        "use_chunk_days": cfg.use_chunk_days,
        "chunk_days": cfg.chunk_days,
        "consultar_api": cfg.consultar_api,
        "login_type": normalize_login_type(cfg.login_type),
        "tipo_nota": normalize_tipo_nota(cfg.tipo_nota),
    }


def _alias_to_client_name(alias: str) -> str:
    alias = (alias or "").strip()
    if not alias:
        return "Cliente"
    if " - " in alias:
        return alias.split(" - ", 1)[1].strip() or alias
    return alias


def _alias_to_client_id(alias: str) -> str:
    import re
    value = re.sub(r"[^a-z0-9]+", "-", _alias_to_client_name(alias).lower()).strip("-")
    return value or "cliente"


def _ultimos_dias(dias: int | None = 30) -> tuple[date, date]:
    """Retorna a janela automatica encerrada em ontem."""
    hoje = today_sao_paulo()
    dias_normalizados = int(dias or 30)
    if dias_normalizados < 1:
        dias_normalizados = 30
    fim = hoje - timedelta(days=1)
    inicio = fim - timedelta(days=dias_normalizados - 1)
    return inicio, fim


def _calcular_proxima_execucao_horario(hora_str: str) -> datetime:
    """Retorna a próxima ocorrência futura válida para o horário informado."""
    hora_h, hora_m = map(int, hora_str.split(":"))
    agora = now_sao_paulo()
    alvo = agora.replace(hour=hora_h, minute=hora_m, second=0, microsecond=0)
    if alvo <= agora:
        alvo += timedelta(days=1)
    return alvo


def _get_aliases_validos(login_type: LoginTypeEnum) -> set:
    """Retorna o conjunto de aliases válidos conforme o tipo de login."""
    if login_type == LoginTypeEnum.cpf_cnpj:
        creds = carregar_credenciais(str(settings.credentials_json_path))
        return {c.get("alias") for c in creds if c.get("alias")}
    else:
        certs = carregar_certificados(str(settings.certs_json_path))
        return {c.get("alias") for c in certs if c.get("alias")}


def _is_tmp_runtime_path(path_value: Path) -> bool:
    normalized = str(path_value).replace("\\", "/").lower()
    return normalized == "/tmp" or normalized.startswith("/tmp/") or normalized.endswith(":/tmp") or "/tmp/" in normalized


def _log_runtime_storage_info() -> None:
    runtime_info = {
        "APP_ENV": settings.app_env,
        "APP_DATA_DIR": settings.app_data_dir,
        "CERTS_JSON_PATH": settings.certs_json_path,
        "CREDENTIALS_JSON_PATH": settings.credentials_json_path,
        "SECRETS_FILE_PATH": settings.secrets_file_path,
        "OUTPUT_DIR": settings.output_dir,
        "TEMP_DIR": settings.temp_dir,
    }

    logger.info("Runtime storage configuration:")
    for key, value in runtime_info.items():
        logger.info("  %s=%s", key, value)

    if settings.app_env == "production":
        tmp_entries = [
            f"{key}={value}"
            for key, value in runtime_info.items()
            if key != "APP_ENV" and _is_tmp_runtime_path(Path(value))
        ]
        if tmp_entries:
            logger.warning(
                "Storage efemero detectado em producao. Os caminhos abaixo apontam para /tmp e podem causar perda de dados apos restart/redeploy: %s",
                ", ".join(tmp_entries),
            )


# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
def ensure_directories():
    for directory in (BASE_DATA_DIR, OUTPUT_DIR, TEMP_DIR, CERTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
def startup_event():
    try:
        settings.validate(require_database=True)
        settings.ensure_runtime_dirs()
        ensure_directories()
        _log_runtime_storage_info()
        ensure_database_extensions()
        garantir_schema_nfse_certificados()
        garantir_schema_nfse_certificados_segredos()
        garantir_schema_nfse_dispatch_queue()
        garantir_schema_nfse_notas()
        garantir_schema_nfse_execucoes()
        migrados = migrar_certificados_legados(settings.certs_json_path)
        if migrados:
            logger.info("Migracao automatica de certificados legados concluida: %s certificado(s).", migrados)
        start_dispatcher()
    except Exception as exc:
        raise RuntimeError(f"Falha crítica no startup da API: {exc}") from exc

    # Restaurar agendamentos que estavam ativos antes da última reinicialização
    def _factory(row: dict):
        """Reconstrói a função de execução a partir do payload salvo."""
        payload = row.get("payload_json") or {}
        restored_job_id = str(row.get("job_id") or "")
        try:
            # Compatibilidade: payload antigo pode ter base_dir, ignoramos
            payload_clean = {k: v for k, v in payload.items() if k != 'base_dir'}
            req = ExecRequest(**payload_clean)
            logger.info(
                "Payload de agendamento restaurado",
                extra={
                    "cert_aliases": list(req.cert_aliases),
                    "lookback_days": req.lookback_days,
                    "use_chunk_days": req.use_chunk_days,
                    "chunk_days": req.chunk_days,
                    "hora_execucao": req.hora_execucao,
                },
            )
        except Exception:
            return None

        def executar():
            inicio, fim = _ultimos_dias(req.lookback_days)
            execution_id = str(uuid.uuid4())
            aliases = _get_aliases_validos(req.login_type)
            slot_key = f"{inicio.isoformat()}:{fim.isoformat()}"
            with scheduler_dispatch_guard(restored_job_id or execution_id, slot_key) as locked:
                if not locked:
                    logger.info("Agendamento restaurado ignorado: slot ja enfileirado por outra instancia.")
                    return
                for alias in req.cert_aliases:
                    if alias not in aliases:
                        continue
                    proc_create = ProcessoCreate(
                        execution_id=execution_id,
                        cert_alias=alias,
                        login_type=req.login_type,
                        tipo_nota=req.tipo_nota,
                        start_date=inicio,
                        end_date=fim,
                    )
                    proc_id = criar_processo(proc_create)
                    exec_payload = {
                        **payload,
                        "start": inicio.isoformat(),
                        "end": fim.isoformat(),
                        "agendado": True,
                        "hora_execucao": req.hora_execucao,
                    }
                    criar_execucao(execution_id, proc_id, exec_payload)
                    cfg = _build_automatic_run_config(req, alias, inicio, fim)
                    enqueue_dispatch_item(
                        job_id=execution_id,
                        processo_id=proc_id,
                        cert_alias=alias,
                        payload_json=_queue_payload_from_config(cfg, alias),
                    )

        return executar

    def _restore_first_run_at(row: dict) -> datetime | None:
        payload = row.get("payload_json") or {}
        hora_str = str(payload.get("hora_execucao") or "06:00")
        try:
            return _calcular_proxima_execucao_horario(hora_str)
        except Exception:
            logger.warning("hora_execucao invalido no restore; fallback para proxima execucao imediata", extra={"job_id": row.get("job_id"), "hora_execucao": hora_str})
            return None

    restaurados = restaurar_agendamentos_do_banco(_factory, first_run_at_resolver=_restore_first_run_at)
    if restaurados:
        print(f"[API] {restaurados} agendamento(s) restaurado(s) do banco.")

    # Agendar limpeza diária do MinIO (executa a cada 24h)
    def _limpar_minio():
        resultado = limpar_arquivos_antigos_minio(dias=15)
        print(f"[MinIO] Limpeza diária: {resultado['removidos']} arquivo(s) removido(s)")

    iniciar_agendamento(
        job_id="__minio_cleanup__",
        func=_limpar_minio,
        intervalo_segundos=86400,
        descricao="Limpeza automática MinIO (15 dias)",
    )


# ─── Health ───────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "ok",
    }


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.app_env,
        "timestamp": now_utc().isoformat(),
        "dispatcher": get_dispatcher_debug_snapshot(),
    }


@app.api_route("/health/db", methods=["GET", "HEAD"])
def health_db():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
        return {
            "status": "ok",
            "database": "ok",
            "result": row["?column?"] if isinstance(row, dict) and "?column?" in row else 1,
            "timestamp": now_utc().isoformat(),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database_unavailable: {exc}")


# ─── Certificados ─────────────────────────────────────────────────────────────

@app.get("/certificados")
def listar_certificados():
    certificados = carregar_certificados(str(settings.certs_json_path))
    items = []
    for c in certificados:
        alias = c.get("alias")
        if not alias:
            continue
        items.append({
            "id":          alias,
            "alias":       alias,
            "cert_alias":  alias,
            "client_name": _alias_to_client_name(alias),
            "client_id":   _alias_to_client_id(alias),
            "file_name":   certificate_display_name(c),
            "status":      "valid",
        })
    return {"certificados": items}


@app.post("/certificados", status_code=201)
async def criar_certificado(
    alias: str = Form(...),
    client_name: str = Form(...),
    password: str = Form(...),
    file: UploadFile = File(...),
):
    try:
        content = await file.read()
        cert = adicionar_certificado(
            alias=alias,
            client_name=client_name,
            pfx_bytes=content,
            password=password,
            original_filename=file.filename,
        )
        return {"success": True, "certificado": cert}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/certificados/{alias}")
def atualizar_certificado(alias: str, data: CertificadoEdit):
    try:
        result = editar_certificado(
            alias=alias,
            novo_alias=data.novo_alias,
            client_name=data.client_name,
        )
        return {"success": True, "certificado": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/certificados/{alias}/senha")
def redefinir_senha_cert(alias: str, data: SenhaUpdate):
    try:
        redefinir_senha_certificado(alias, data.password)
        return {"success": True, "message": f"Senha do certificado '{alias}' redefinida com sucesso."}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/certificados/{alias}")
def deletar_certificado(alias: str):
    try:
        excluir_certificado(alias)
        return {"success": True, "message": f"Certificado '{alias}' excluído com sucesso."}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Credenciais ──────────────────────────────────────────────────────────────

@app.get("/credenciais")
def listar_credenciais():
    creds = carregar_credenciais(str(settings.credentials_json_path))
    items = []
    for c in creds:
        alias = c.get("alias")
        if not alias:
            continue
        items.append({
            "id":          alias,
            "alias":       alias,
            "client_name": _alias_to_client_name(alias),
            "client_id":   _alias_to_client_id(alias),
            "document":    c.get("cpf_cnpj"),
            "status":      "active",
            "has_password": True,
        })
    return {"credenciais": items}


@app.post("/credenciais", status_code=201)
def criar_credencial(data: CredencialCreate):
    if not validar_cpf_cnpj(data.cpf_cnpj):
        raise HTTPException(
            status_code=422,
            detail=f"CPF/CNPJ inválido: '{data.cpf_cnpj}'. Informe um CPF (11 dígitos) ou CNPJ (14 dígitos) válido."
        )
    try:
        cred = adicionar_credencial(
            alias=data.alias,
            cpf_cnpj=data.cpf_cnpj,
            password=data.password,
        )
        return {"success": True, "credencial": cred}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/credenciais/{alias}")
def atualizar_credencial(alias: str, data: CredencialEdit):
    try:
        result = editar_credencial(
            alias=alias,
            novo_alias=data.novo_alias,
            cpf_cnpj=data.cpf_cnpj,
        )
        return {"success": True, "credencial": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/credenciais/{alias}/senha")
def redefinir_senha_cred(alias: str, data: SenhaUpdate):
    try:
        redefinir_senha_credencial(alias, data.password)
        return {"success": True, "message": f"Senha da credencial '{alias}' redefinida com sucesso."}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/credenciais/{alias}")
def deletar_credencial(alias: str):
    try:
        excluir_credencial(alias)
        return {"success": True, "message": f"Credencial '{alias}' excluída com sucesso."}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Execução ─────────────────────────────────────────────────────────────────

@app.post("/executar")
def executar(req: ExecRequest):
    _validar_intervalo_execucao(req.start, req.end)

    logger.info(
        "Requisicao /executar recebida",
        extra={
            "cert_aliases": list(req.cert_aliases),
            "lookback_days": req.lookback_days,
            "start": req.start.isoformat(),
            "end": req.end.isoformat(),
            "use_chunk_days": req.use_chunk_days,
            "chunk_days": req.chunk_days,
            "login_type": str(req.login_type),
            "tipo_nota": normalize_tipo_nota(req.tipo_nota),
        },
    )
    aliases_validos = _get_aliases_validos(req.login_type)
    invalidos = [a for a in req.cert_aliases if a not in aliases_validos]
    if invalidos:
        raise HTTPException(status_code=400, detail=f"Aliases inválidos: {', '.join(invalidos)}")

    job_id = str(uuid.uuid4())
    processos = []

    for alias in req.cert_aliases:
        proc_id = criar_processo(ProcessoCreate(
            execution_id=job_id,
            cert_alias=alias,
            login_type=req.login_type,
            tipo_nota=req.tipo_nota,
            start_date=req.start,
            end_date=req.end,
        ))
        criar_execucao(job_id, proc_id, req.model_dump(mode="json"))
        logger.info(
            "Execucao persistida",
            extra={
                "job_id": job_id,
                "processo_id": proc_id,
                "cert_alias": alias,
                "use_chunk_days": req.use_chunk_days,
                "chunk_days": req.chunk_days,
                "start": req.start.isoformat(),
                "end": req.end.isoformat(),
            },
        )

        cfg = _build_run_config(req, alias)
        enqueue_dispatch_item(
            job_id=job_id,
            processo_id=proc_id,
            cert_alias=alias,
            payload_json=_queue_payload_from_config(cfg, alias),
        )
        processos.append({"processo_id": proc_id, "cert_alias": alias})

    return {"job_id": job_id, "status": "queued", "processos": processos}


@app.post("/agendar")
def agendar_execucao(req: ExecRequest):
    """
    Ativa o modo automático diário.

    - O campo `hora_execucao` (HH:MM) define o horário exato de disparo todo dia.
    - Se o horário já passou hoje, a primeira execução será amanhã nesse horário.
    - Se o horário ainda não chegou hoje, a primeira execução será hoje.
    - A cada execução o período é calculado como os últimos 30 dias corridos.
    """
    _validar_intervalo_execucao(req.start, req.end)

    aliases_validos = _get_aliases_validos(req.login_type)
    invalidos = [a for a in req.cert_aliases if a not in aliases_validos]
    if invalidos:
        raise HTTPException(status_code=400, detail=f"Aliases inválidos: {', '.join(invalidos)}")

    # Validar formato hora_execucao
    try:
        hora_str = req.hora_execucao or "06:00"
        hora_h, hora_m = map(int, hora_str.split(":"))
        if not (0 <= hora_h <= 23 and 0 <= hora_m <= 59):
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail=f"hora_execucao inválido: '{req.hora_execucao}'. Use o formato HH:MM (ex: 06:00)")

    job_id = str(uuid.uuid4())
    payload = req.model_dump(mode="json")
    logger.info(
        "Requisicao /agendar recebida",
        extra={
            "job_id": job_id,
            "cert_aliases": list(req.cert_aliases),
            "lookback_days": req.lookback_days,
            "use_chunk_days": req.use_chunk_days,
            "chunk_days": req.chunk_days,
            "hora_execucao": req.hora_execucao,
            "login_type": str(req.login_type),
            "tipo_nota": normalize_tipo_nota(req.tipo_nota),
        },
    )

    def _calcular_proxima_execucao() -> datetime:
        return _calcular_proxima_execucao_horario(hora_str)

    def _executar_agendado_sem_espera() -> None:
        inicio, fim = _ultimos_dias(req.lookback_days)
        execution_id = str(uuid.uuid4())
        print(f"[AGENDAMENTO {job_id}] Iniciando processamento — período: {inicio} a {fim}")

        slot_key = f"{inicio.isoformat()}:{fim.isoformat()}"
        with scheduler_dispatch_guard(job_id, slot_key) as locked:
            if not locked:
                print(f"[AGENDAMENTO {job_id}] Disparo ignorado: outro backend ja enfileirou este slot.")
                return

            for alias in req.cert_aliases:
                proc_id = criar_processo(ProcessoCreate(
                    execution_id=execution_id,
                    cert_alias=alias,
                    login_type=req.login_type,
                    tipo_nota=req.tipo_nota,
                    start_date=inicio,
                    end_date=fim,
                ))

                exec_payload = {
                    **payload,
                    "start": inicio.isoformat(),
                    "end": fim.isoformat(),
                    "agendado": True,
                    "hora_execucao": hora_str,
                }
                criar_execucao(execution_id, proc_id, exec_payload)
                logger.info(
                    "Execucao agendada persistida",
                    extra={
                        "execution_id": execution_id,
                        "processo_id": proc_id,
                        "cert_alias": alias,
                        "lookback_days": exec_payload.get("lookback_days"),
                        "use_chunk_days": exec_payload.get("use_chunk_days"),
                        "chunk_days": exec_payload.get("chunk_days"),
                        "start": exec_payload.get("start"),
                        "end": exec_payload.get("end"),
                    },
                )

                cfg = RunConfig(
                    modo="manual",
                    base_dir=_get_data_dir(alias),
                    certs_json_path=str(settings.certs_json_path),
                    credentials_json_path=str(settings.credentials_json_path),
                    cert_aliases=[alias],
                    start=inicio,
                    end=fim,
                    headless=req.headless,
                    lookback_days=req.lookback_days,
                    use_chunk_days=req.use_chunk_days,
                    chunk_days=req.chunk_days,
                    consultar_api=req.consultar_api,
                    login_type=req.login_type,
                    tipo_nota=normalize_tipo_nota(req.tipo_nota),
                )
                enqueue_dispatch_item(
                    job_id=execution_id,
                    processo_id=proc_id,
                    cert_alias=alias,
                    payload_json=_queue_payload_from_config(cfg, alias),
                )

    first_run_at = _calcular_proxima_execucao()

    iniciar_agendamento(
        job_id=job_id,
        func=_executar_agendado_sem_espera,
        intervalo_segundos=86400,
        descricao=f"Automático diário {hora_str} — últimos {req.lookback_days} dias — {', '.join(req.cert_aliases)}",
        payload=payload,
        first_run_at=first_run_at,
    )

    proxima = first_run_at
    inicio, fim = _ultimos_dias(req.lookback_days)
    return {
        "success": True,
        "job_id": job_id,
        "tipo": "automatico_diario",
        "hora_execucao": hora_str,
        "intervalo_segundos": 86400,
        "descricao": f"Últimos {req.lookback_days} dias corridos, todo dia às {hora_str}",
        "lookback_days": req.lookback_days,
        "proxima_execucao": proxima.isoformat(),
        "periodo_proximo": {"start": inicio.isoformat(), "end": fim.isoformat()},
    }


# ─── Agendamentos ─────────────────────────────────────────────────────────────

@app.get("/agendamentos")
def listar_jobs():
    return {"jobs": listar_agendamentos()}


@app.delete("/agendamentos/{job_id}")
def parar_job(job_id: str):
    parar_agendamento(job_id)
    return {"success": True}


# ─── Status ───────────────────────────────────────────────────────────────────

@app.get("/status/{job_id}")
def status_job(job_id: str):
    exec_data = obter_execucao(job_id)
    if not exec_data:
        raise HTTPException(status_code=404, detail="job_id não encontrado")
    processos = listar_processos(execution_id=job_id, page=1, page_size=100)
    return {
        "job_id": job_id,
        "status": exec_data["status"],
        "processos": [
            {"processo_id": p.id, "cert_alias": p.cert_alias, "status": p.status}
            for p in processos
        ],
    }


@app.post("/processos/{processo_id}/cancel")
def cancel_processo(processo_id: str):
    proc = obter_processo(processo_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Processo não encontrado")

    if proc.status in {StatusEnum.completed, StatusEnum.failed, StatusEnum.cancelled}:
        logger.info(
            "Cancelamento ignorado para processo terminal. processo=%s status=%s",
            processo_id,
            proc.status,
        )
        return {
            "success": True,
            "processo_id": processo_id,
            "status": proc.status,
            "message": f"Processo já está em estado terminal: {proc.status}.",
            "dispatch": {"queued_cancelled": 0, "running_signalled": 0},
        }

    message = "Processo cancelado manualmente via API."
    logger.warning(
        "Cancelamento manual solicitado para processo=%s status_atual=%s",
        processo_id,
        proc.status,
    )
    atualizar_status_processo(
        processo_id,
        StatusEnum.cancelled,
        finished_at=now_utc(),
        error_message=message,
    )
    atualizar_status_execucao(
        processo_id,
        "cancelled",
        finished_at=now_utc(),
        error=message,
        traceback="api_manual_cancel",
    )
    dispatch_result = cancel_dispatch_items_for_process(processo_id, reason=message)
    logger.warning(
        "Cancelamento manual solicitado para processo=%s queued_cancelled=%s running_signalled=%s",
        processo_id,
        dispatch_result["queued_cancelled"],
        dispatch_result["running_signalled"],
    )
    logger.info("Processo %s marcado como cancelled com sucesso.", processo_id)
    return {
        "success": True,
        "processo_id": processo_id,
        "status": StatusEnum.cancelled,
        "message": message,
        "dispatch": dispatch_result,
    }


@app.delete("/processos/{processo_id}")
def delete_processo(processo_id: str):
    proc = obter_processo(processo_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Processo não encontrado")

    if proc.status in {StatusEnum.queued, StatusEnum.running}:
        logger.warning(
            "Exclusão bloqueada para processo ativo. processo=%s status=%s",
            processo_id,
            proc.status,
        )
        raise HTTPException(
            status_code=409,
            detail=f"Processo com status '{proc.status}' não pode ser excluído. Cancele-o antes de excluir.",
        )

    garantir_schema_nfse_execucoes()
    garantir_schema_nfse_notas()
    garantir_schema_nfse_processo_arquivos()
    garantir_schema_nfse_dispatch_queue()

    logger.warning(
        "Exclusão manual solicitada para processo=%s status=%s",
        processo_id,
        proc.status,
    )

    try:
        with get_conn() as conn:
            related = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM nfse_dispatch_queue WHERE processo_id = %s) AS dispatch_total,
                  (SELECT COUNT(*) FROM nfse_dispatch_queue WHERE processo_id = %s AND status IN ('queued', 'running')) AS dispatch_ativos,
                  (SELECT COUNT(*) FROM nfse_execucoes WHERE processo_id = %s) AS execucoes_total,
                  (SELECT COUNT(*) FROM nfse_processo_arquivos WHERE processo_id = %s) AS arquivos_total,
                  (SELECT COUNT(*) FROM nfse_processo_notas WHERE processo_id = %s) AS vinculos_notas_total,
                  (SELECT COUNT(*) FROM nfse_notas WHERE processo_id = %s) AS notas_diretas_total
                """,
                (processo_id, processo_id, processo_id, processo_id, processo_id, processo_id),
            ).fetchone()

            dispatch_ativos = int((related or {}).get("dispatch_ativos") or 0)
            if dispatch_ativos > 0:
                logger.warning(
                    "Exclusão bloqueada por itens ativos na fila. processo=%s dispatch_ativos=%s",
                    processo_id,
                    dispatch_ativos,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Processo possui item(ns) ativos na fila. Cancele o processo e aguarde a fila finalizar o cancelamento antes de excluir.",
                )

            deleted_dispatch = conn.execute(
                "DELETE FROM nfse_dispatch_queue WHERE processo_id = %s",
                (processo_id,),
            ).rowcount or 0
            deleted_execucoes = conn.execute(
                "DELETE FROM nfse_execucoes WHERE processo_id = %s",
                (processo_id,),
            ).rowcount or 0
            deleted_vinculos_notas = conn.execute(
                "DELETE FROM nfse_processo_notas WHERE processo_id = %s",
                (processo_id,),
            ).rowcount or 0
            detached_notas = conn.execute(
                """
                UPDATE nfse_notas
                SET processo_id = NULL,
                    updated_at = now()
                WHERE processo_id = %s
                """,
                (processo_id,),
            ).rowcount or 0
            deleted_processos = conn.execute(
                "DELETE FROM nfse_processos WHERE id = %s",
                (processo_id,),
            ).rowcount or 0

        if deleted_processos == 0:
            logger.warning("Exclusão abortada: processo %s sumiu durante a operação.", processo_id)
            raise HTTPException(status_code=404, detail="Processo não encontrado para exclusão")

        logger.info(
            "Exclusão concluída para processo=%s dispatch=%s execucoes=%s vinculos_notas=%s notas_desvinculadas=%s arquivos_em_cascata=%s",
            processo_id,
            deleted_dispatch,
            deleted_execucoes,
            deleted_vinculos_notas,
            detached_notas,
            int((related or {}).get("arquivos_total") or 0),
        )
        return {
            "success": True,
            "processo_id": processo_id,
            "status_anterior": proc.status,
            "message": "Processo excluído com sucesso. Notas foram preservadas e apenas desvinculadas do processo.",
            "deleted": {
                "processos": deleted_processos,
                "dispatch_items": deleted_dispatch,
                "execucoes": deleted_execucoes,
                "processo_notas": deleted_vinculos_notas,
                "arquivos_metadados_em_cascata": int((related or {}).get("arquivos_total") or 0),
            },
            "detached": {
                "notas_com_processo_id_nulo": detached_notas,
            },
            "storage_cleanup": {
                "remote_objects_removed": False,
                "local_files_removed": False,
                "reason": "comportamento_conservador_para_evitar_apagar_arquivos_físicos_compartilhados_ou_ainda_úteis",
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Falha ao excluir processo=%s", processo_id)
        raise HTTPException(status_code=500, detail=f"Falha ao excluir processo: {exc}")


# ─── Execuções ────────────────────────────────────────────────────────────────

@app.get("/execucoes", response_model=dict)
def get_execucoes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    data = listar_execucoes(page=page, page_size=page_size)
    items = []
    for row in data["items"]:
        payload = row.get("payload_json") or {}
        aliases = row.get("aliases") or []
        started_at  = row.get("started_at")  or row.get("created_at")
        finished_at = row.get("finished_at")
        duration = None
        if started_at and finished_at:
            delta = finished_at - started_at
            secs  = int(delta.total_seconds())
            duration = f"{secs // 60}m {secs % 60}s"
        items.append({
            "id":               row["job_id"],
            "job_id":           row["job_id"],
            "client_name":      _alias_to_client_name((aliases or ["Execução"])[0]),
            "client_id":        _alias_to_client_id((aliases or ["Execução"])[0]),
            "aliases":          aliases,
            "login_type":       "credential" if payload.get("login_type") == "cpf_cnpj" else "certificate",
            "mode":             "automatico" if payload.get("agendado") else "manual",
            "period_start":     payload.get("start"),
            "period_end":       payload.get("end"),
            "lookback_days":    payload.get("lookback_days", 30),
            "use_chunk_days":   payload.get("use_chunk_days", False),
            "chunk_days":       payload.get("chunk_days"),
            "status":           row.get("status"),
            "started_at":       started_at,
            "finished_at":      finished_at,
            "created_at":       row.get("created_at"),
            "duration":         duration,
            "errors":           row.get("processos_falhos", 0),
            "total_found":      row.get("total_processos", 0),
            "total_processed":  row.get("processos_concluidos", 0),
            "message":          row.get("error_message") or f"{row.get('processos_concluidos', 0)} de {row.get('total_processos', 0)} processos concluídos",
            "messages":         [m for m in [row.get("error_message")] if m],
        })
    return {**data, "items": items}


# ─── NFS-e ────────────────────────────────────────────────────────────────────

@app.get("/nfse", response_model=dict)
def get_nfse(
    cert_alias: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    status_fila: Optional[str] = Query(None),
    status_fila_manual: Optional[str] = Query(None),
    prioridade_manual: Optional[str] = Query(None),
    responsavel: Optional[str] = Query(None),
    municipio: Optional[str] = Query(None),
    cnpj_cpf: Optional[str] = Query(None),
    competencia: Optional[str] = Query(None),
    codigo_servico: Optional[str] = Query(None),
    data_tipo: Optional[str] = Query(None),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    somente_divergentes: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=10000),
    pageSize: Optional[int] = Query(None, ge=1, le=10000),
):
    if not isinstance(page_size, int):
        page_size = None
    if not isinstance(pageSize, int):
        pageSize = None

    resolved_page_size = pageSize if pageSize is not None else page_size
    if resolved_page_size is None:
        resolved_page_size = 200

    filters = {
        "cert_alias": cert_alias, "status": status, "status_fila": status_fila,
        "status_fila_manual": status_fila_manual, "prioridade_manual": prioridade_manual,
        "responsavel": responsavel, "municipio": municipio,
        "cnpj_cpf": cnpj_cpf, "competencia": competencia,
        "codigo_servico": codigo_servico, "somente_divergentes": somente_divergentes,
        "data_tipo": data_tipo, "data_inicio": data_inicio, "data_fim": data_fim,
    }
    items, total = listar_notas_agrupadas(filters, page=page, page_size=resolved_page_size)
    fila_metadata = listar_empresas_e_contadores_fila(filters)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": resolved_page_size,
        "empresas_disponiveis": fila_metadata["empresas"],
        "total_empresas": fila_metadata["total_empresas"],
        "contadores": fila_metadata["contadores"],
    }


@app.get("/fila-regras-atribuicao", response_model=list[RegraAtribuicaoResponse])
def get_fila_regras_atribuicao():
    return listar_regras_atribuicao()


@app.post("/fila-regras-atribuicao", response_model=RegraAtribuicaoResponse)
def post_fila_regra_atribuicao(data: RegraAtribuicaoCreate):
    return criar_regra_atribuicao(
        campo=data.campo,
        operador=data.operador,
        valor=data.valor,
        responsavel=data.responsavel,
        prioridade=data.prioridade,
        ativo=data.ativo,
    )


@app.put("/fila-regras-atribuicao/{regra_id}", response_model=RegraAtribuicaoResponse)
def put_fila_regra_atribuicao(regra_id: int, data: RegraAtribuicaoUpdate):
    row = atualizar_regra_atribuicao(
        regra_id=regra_id,
        campo=data.campo,
        operador=data.operador,
        valor=data.valor,
        responsavel=data.responsavel,
        prioridade=data.prioridade,
        ativo=data.ativo,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Regra {regra_id} não encontrada")
    return row


@app.delete("/fila-regras-atribuicao/{regra_id}")
def delete_fila_regra_atribuicao(regra_id: int):
    ok = excluir_regra_atribuicao(regra_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Regra {regra_id} não encontrada")
    return {"success": True, "id": regra_id}


@app.post("/fila-regras-atribuicao/reaplicar")
def post_fila_regras_reaplicar(somente_sem_responsavel: bool = Query(True)):
    atualizadas = reaplicar_regras_atribuicao(only_empty=somente_sem_responsavel)
    return {"success": True, "atualizadas": atualizadas}


@app.put("/nfse/{nota_id}")
def atualizar_nota(nota_id: int, data: NotaEditRequest):
    """
    Permite ao auditor salvar edições nos campos editáveis do relatório interativo:
    - valor_liquido_correto: valor correto calculado/corrigido manualmente
    - alertas_fiscais: anotações e alertas do auditor

    O status_valor_liquido é recalculado automaticamente.
    """
    ok = atualizar_nota_campos_editaveis(
        nota_id=nota_id,
        valor_liquido_correto=data.valor_liquido_correto,
        alertas_fiscais=data.alertas_fiscais,
        observacao_interna=data.observacao_interna,
        status_fila_manual=data.status_fila_manual,
        prioridade_manual=data.prioridade_manual,
        responsavel=data.responsavel,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"Nota {nota_id} não encontrada")
    return {"success": True, "nota_id": nota_id}


def _serialize_nota_documento(item: dict | None) -> Optional[NotaDocumentoItem]:
    if not item:
        return None
    processo_id = str(item.get("processo_id"))
    arquivo_id = int(item.get("id"))
    return NotaDocumentoItem(
        id=arquivo_id,
        processo_id=processo_id,
        tipo_arquivo=item.get("tipo_arquivo"),
        nome_arquivo=item.get("nome_arquivo"),
        content_type=item.get("content_type"),
        view_url=f"/processos/{processo_id}/arquivos/{arquivo_id}/view",
        download_url=f"/processos/{processo_id}/arquivos/{arquivo_id}/download",
    )


@app.get("/nfse/{nota_id}/documentos", response_model=NotaDocumentosResponse)
def get_nfse_documentos(nota_id: int):
    docs = localizar_documentos_nota(nota_id)
    return NotaDocumentosResponse(
        nota_id=nota_id,
        processo_id=docs.get("processo_id"),
        xml=_serialize_nota_documento(docs.get("xml")),
        pdf=_serialize_nota_documento(docs.get("pdf")),
    )


@app.get("/processos", response_model=dict)
def get_processos(
    cert_alias: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=500),
):
    items = listar_processos(cert_alias=cert_alias, status=status, page=page, page_size=page_size)

    params = []
    where_clauses = []
    if cert_alias:
        where_clauses.append("cert_alias = %s")
        params.append(cert_alias)
    if status:
        where_clauses.append("status = %s")
        params.append(status)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with get_conn() as conn:
        total_row = conn.execute(
            f"SELECT COUNT(*) as total FROM nfse_processos {where}", params
        ).fetchone()
        total = total_row["total"] if total_row else 0

    return {
        "items": [item.model_dump() for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/processos/{processo_id}", response_model=ProcessoResponse)
def get_processo(processo_id: str):
    proc = obter_processo(processo_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Processo não encontrado")
    return proc


@app.get("/processos/{processo_id}/pdfs", response_model=List[ArquivoResponse])
def get_pdfs(processo_id: str):
    return listar_arquivos_processo(processo_id, "pdf")


@app.get("/processos/{processo_id}/xmls", response_model=List[ArquivoResponse])
def get_xmls(processo_id: str):
    return listar_arquivos_processo(processo_id, "xml")


@app.get("/processos/{processo_id}/planilhas", response_model=List[ArquivoResponse])
def get_planilhas(processo_id: str):
    return listar_arquivos_processo(processo_id, "relatorio")


@app.get("/processos/{processo_id}/relatorio", response_model=dict)
def get_relatorio(
    processo_id: str,
    status: Optional[str] = Query(None),
    municipio: Optional[str] = Query(None),
    cnpj_cpf: Optional[str] = Query(None),
    competencia: Optional[str] = Query(None),
    codigo_servico: Optional[str] = Query(None),
    somente_divergentes: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    filters = {
        "status": status, "municipio": municipio, "cnpj_cpf": cnpj_cpf,
        "competencia": competencia, "codigo_servico": codigo_servico,
        "somente_divergentes": somente_divergentes,
    }
    items, total = listar_notas_por_processo(processo_id, filters, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/processos/{processo_id}/summary", response_model=SummaryResponse)
def get_summary(processo_id: str):
    resumo = obter_resumo_processo(processo_id)
    return SummaryResponse(**resumo)


@app.get("/relatorios/processo/{processo_id}", response_model=dict)
def get_relatorio_processo(processo_id: str):
    return gerar_relatorio_processo(processo_id)


@app.get("/processos/{processo_id}/arquivos/{arquivo_id}/download")
def download_arquivo(processo_id: str, arquivo_id: int):
    arq = obter_arquivo_processo(arquivo_id)
    return _arquivo_redirect_or_file(arq, processo_id, inline=False)


@app.get("/processos/{processo_id}/arquivos/{arquivo_id}/view")
def view_arquivo(processo_id: str, arquivo_id: int):
    arq = obter_arquivo_processo(arquivo_id)
    return _arquivo_redirect_or_file(arq, processo_id, inline=True)


def _arquivo_redirect_or_file(arq, processo_id: str, inline: bool = False):
    if not arq or arq.processo_id != processo_id:
        raise HTTPException(status_code=404, detail="Arquivo n?o encontrado")

    if arq.storage_key and is_s3_configured():
        url = generate_presigned_download_url(arq.storage_key)
        if url:
            return RedirectResponse(url)

    if arq.caminho_local and Path(arq.caminho_local).exists():
        if inline:
            return FileResponse(arq.caminho_local, media_type=arq.content_type or None)
        return FileResponse(arq.caminho_local, filename=arq.nome_arquivo)

    raise HTTPException(status_code=404, detail="Arquivo n?o dispon?vel (n?o est? no MinIO nem localmente)")


def _buscar_conteudo_arquivo(arq) -> tuple:
    """Busca conteúdo de um arquivo do MinIO ou disco local. Retorna (arq, conteudo)."""
    conteudo = None
    if arq.storage_key and is_s3_configured():
        try:
            from modules.storage import get_s3_client, get_s3_settings
            s3 = get_s3_client()
            bucket = get_s3_settings()["bucket"]
            obj = s3.get_object(Bucket=bucket, Key=arq.storage_key)
            conteudo = obj["Body"].read()
        except Exception:
            conteudo = None
    if conteudo is None and arq.caminho_local:
        local = Path(arq.caminho_local)
        if local.exists():
            conteudo = local.read_bytes()
    return (arq, conteudo)


def _gerar_zip_stream(arquivos, nome_zip: str):
    """
    Gerador que produz chunks do ZIP conforme os arquivos são baixados
    em paralelo. Usa ZIP_STORED para PDFs (já comprimidos) e
    ZIP_DEFLATED para XML/planilhas.
    """
    PASTA = {"pdf": "pdf", "xml": "xml", "relatorio": "planilhas"}
    COMPRESSAO = {"pdf": zipfile.ZIP_STORED, "xml": zipfile.ZIP_DEFLATED, "relatorio": zipfile.ZIP_DEFLATED}
    MAX_WORKERS = min(8, len(arquivos))

    # Busca todos os arquivos em paralelo
    resultados = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_buscar_conteudo_arquivo, arq): arq for arq in arquivos}
        for future in as_completed(futures):
            arq, conteudo = future.result()
            if conteudo is not None:
                resultados[arq.id] = (arq, conteudo)

    if not resultados:
        return

    # Monta o ZIP com os arquivos já em memória
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for arq, conteudo in resultados.values():
            pasta = PASTA.get(arq.tipo_arquivo, "outros")
            comp  = COMPRESSAO.get(arq.tipo_arquivo, zipfile.ZIP_DEFLATED)
            zf.writestr(
                zipfile.ZipInfo(f"{pasta}/{arq.nome_arquivo}"),
                conteudo,
                compress_type=comp,
            )
    buf.seek(0)
    yield buf.read()


@app.get("/processos/{processo_id}/download-zip")
def download_zip(processo_id: str):
    """
    Empacota todos os arquivos do processo (PDFs + XMLs + planilha) em um .zip
    e retorna como stream para download direto no browser do usuário.
    Busca arquivos do MinIO em paralelo para reduzir latência.
    """
    proc = obter_processo(processo_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Processo não encontrado")

    arquivos = listar_arquivos_processo(processo_id)
    if not arquivos:
        raise HTTPException(status_code=404, detail="Nenhum arquivo disponível para este processo")

    nome_zip = f"processo_{processo_id[:8]}_{proc.cert_alias.replace(' ', '_')[:30]}.zip"

    return StreamingResponse(
        _gerar_zip_stream(arquivos, nome_zip),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{nome_zip}"'},
    )


@app.get("/processos/{processo_id}/relatorio-csv")
def download_relatorio_csv(processo_id: str):
    """
    Exporta o relatório completo do processo como CSV com todos os campos
    de auditoria no padrão da planilha, com BOM UTF-8 para Excel.
    """
    proc = obter_processo(processo_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Processo não encontrado")

    items, _ = listar_notas_por_processo(processo_id, filters={}, page=1, page_size=10000)
    if not items:
        raise HTTPException(status_code=404, detail="Nenhuma nota encontrada para este processo")

    COLUNAS = [
        ("Competência",             "competencia"),
        ("Município",               "municipio"),
        ("Chave de Acesso",         "chave_acesso"),
        ("Data de Emissão",         "data_emissao"),
        ("CNPJ/CPF",                "cnpj_cpf"),
        ("Razão Social",            "razao_social"),
        ("N° Documento",            "numero_documento"),
        ("Valor Total",             "valor_total"),
        ("Status nota",             "status_nota"),
        ("Status",                  "status"),
        ("Diverg\u00eancia",            "divergencia_fila_label"),
        ("Prioridade",              "prioridade_manual"),
        ("Respons\u00e1vel",            "responsavel"),
        ("Valor B/C",               "valor_base"),
        ("Status Base de Cálculo",  "status_base_calculo"),
        ("CSRF",                    "csrf"),
        ("IRRF",                    "irrf"),
        ("Percentual IRRF",         "percentual_irrf"),
        ("INSS",                    "inss"),
        ("ISS",                     "iss"),
        ("Valor Líquido",           "valor_liquido"),
        ("Valor Líquido Correto",   "valor_liquido_correto"),
        ("Status Valor Líquido",    "status_valor_liquido"),
        ("Incidência do ISS",       "incidencia_iss"),
        ("Data do pagamento",       "data_pagamento"),
        ("Código de serviço",       "codigo_servico"),
        ("Descrição do Serviço",    "descricao_servico"),
        ("Código NBS",              "codigo_nbs"),
        ("Código CNAE",             "cnae"),
        ("Descrição CNAE",          "descricao_cnae"),
        ("Simples Nacional / XML",  "simples_nacional"),
        ("Consulta Simples API",    "consulta_simples_api"),
        ("Status Simples Nacional", "status_simples_nacional"),
        ("Status CSRF",             "status_csrf"),
        ("Status IRRF",             "status_irrf"),
        ("Status INSS",             "status_inss"),
        ("Alertas Fiscais",         "alertas_fiscais"),
        ("dia processado",          "dia_processado"),
    ]

    output = io.StringIO()
    output.write("\ufeff")  # BOM UTF-8 para Excel
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writerow([h for h, _ in COLUNAS])
    for row in items:
        writer.writerow([serialize_export_value(row.get(k)) for _, k in COLUNAS])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    nome_csv = f"relatorio_{proc.cert_alias.replace(' ', '_')[:30]}_{proc.start_date}.csv"

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome_csv}"'},
    )


# ─── Utilitários admin ────────────────────────────────────────────────────────

@app.post("/admin/limpar-minio")
def limpar_minio_manual(dias: int = Query(15, ge=1, le=365)):
    """Aciona manualmente a limpeza de arquivos antigos no MinIO."""
    resultado = limpar_arquivos_antigos_minio(dias=dias)
    return resultado


@app.get("/admin/info")
def info_sistema():
    """Retorna informações sobre o ambiente do servidor."""
    return {
        "version": settings.app_version,
        "environment": settings.app_env,
        "data_dir": _get_data_dir(),
        "certs_json_path": str(settings.certs_json_path),
        "credentials_json_path": str(settings.credentials_json_path),
        "temp_dir": str(settings.temp_dir),
        "s3_configured": is_s3_configured(),
        "timestamp": now_utc().isoformat(),
    }
