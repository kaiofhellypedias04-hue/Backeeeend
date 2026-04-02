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
import time
import uuid
import zipfile
import csv
from pathlib import Path
from datetime import datetime, date, timedelta
from threading import Thread
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from modules.processos_repo import criar_processo, obter_processo, listar_processos
from modules.execucoes_repo import (
    criar_execucao,
    obter_execucao,
    atualizar_status_execucao,
    listar_execucoes,
    garantir_schema_nfse_execucoes,
)
from modules.arquivos_repo import listar_arquivos_processo, obter_arquivo_processo
from modules.notas_repo import (
    garantir_schema_nfse_notas,
    listar_notas_por_processo,
    obter_resumo_processo,
    listar_notas_agrupadas,
    atualizar_nota_campos_editaveis,
    listar_regras_atribuicao,
    criar_regra_atribuicao,
    atualizar_regra_atribuicao,
    excluir_regra_atribuicao,
    reaplicar_regras_atribuicao,
    localizar_documentos_nota,
)
from modules.runner_processos import run_with_process, ProcessRunConfig, RunConfig
from modules.storage import is_s3_configured, generate_presigned_download_url, limpar_arquivos_antigos_minio
from modules.schemas import (
    StatusEnum, LoginTypeEnum, TipoNotaEnum, Pagination,
    ProcessoResponse, ArquivoResponse, NotaReportFilters,
    NotaReportRow, SummaryResponse, ProcessoCreate, NotaDocumentosResponse, NotaDocumentoItem,
    RegraAtribuicaoCreate, RegraAtribuicaoUpdate, RegraAtribuicaoResponse,
)
from modules.reports import gerar_relatorio_processo
from modules.db import get_conn, ensure_database_extensions
from modules.config_loader import carregar_certificados, carregar_credenciais
from modules.export_utils import serialize_export_value
from modules.scheduler import (
    iniciar_agendamento, parar_agendamento, listar_agendamentos,
    restaurar_agendamentos_do_banco,
)
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
    chunk_days: int = 30
    consultar_api: bool = True
    login_type: LoginTypeEnum = LoginTypeEnum.certificado
    tipo_nota: TipoNotaEnum = TipoNotaEnum.tomados
    hora_execucao: str = Field(
        "06:00",
        description="Horário diário de execução no formato HH:MM (usado apenas no modo agendado)",
        pattern=r"^\d{2}:\d{2}$",
    )


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
    return RunConfig(
        modo="manual",
        base_dir=_get_data_dir(cert_alias),
        certs_json_path=str(settings.certs_json_path),
        credentials_json_path=str(settings.credentials_json_path),
        cert_aliases=[cert_alias],
        start=req.start,
        end=req.end,
        headless=req.headless,
        chunk_days=req.chunk_days,
        consultar_api=req.consultar_api,
        login_type=req.login_type,
        tipo_nota=req.tipo_nota,
    )


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


def _ultimos_30_dias() -> tuple[date, date]:
    """Retorna (hoje - 29 dias, hoje) — últimos 30 dias corridos."""
    hoje = date.today()
    return hoje - timedelta(days=29), hoje


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
        garantir_schema_nfse_notas()
        garantir_schema_nfse_execucoes()
    except Exception as exc:
        raise RuntimeError(f"Falha crítica no startup da API: {exc}") from exc

    # Restaurar agendamentos que estavam ativos antes da última reinicialização
    def _factory(payload: dict):
        """Reconstrói a função de execução a partir do payload salvo."""
        try:
            # Compatibilidade: payload antigo pode ter base_dir, ignoramos
            payload_clean = {k: v for k, v in payload.items() if k != 'base_dir'}
            req = ExecRequest(**payload_clean)
        except Exception:
            return None

        def executar():
            inicio, fim = _ultimos_30_dias()
            execution_id = str(uuid.uuid4())
            aliases = _get_aliases_validos(req.login_type)
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
                criar_execucao(execution_id, proc_id, payload)
                cfg = _build_run_config(req, alias)
                pcfg = ProcessRunConfig(
                    **{k: v for k, v in cfg.__dict__.items()},
                    execution_id=execution_id,
                    processo_id=proc_id,
                )
                Thread(target=run_with_process, args=(pcfg,), daemon=True).start()

        return executar

    restaurados = restaurar_agendamentos_do_banco(_factory)
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
        "timestamp": datetime.now().isoformat(),
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
            "timestamp": datetime.now().isoformat(),
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
            "file_name":   Path(c.get("pfxPath") or f"{alias}.pfx").name,
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
    if req.start > req.end:
        raise HTTPException(status_code=400, detail="'start' não pode ser maior que 'end'")

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

        cfg = _build_run_config(req, alias)
        pcfg = ProcessRunConfig(
            **{k: v for k, v in cfg.__dict__.items()},
            execution_id=job_id,
            processo_id=proc_id,
        )
        Thread(target=run_with_process, args=(pcfg,), daemon=True).start()
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

    def _segundos_ate_proximo_horario() -> float:
        """Calcula quantos segundos faltam para o próximo disparo no horário configurado."""
        agora = datetime.now()
        alvo = agora.replace(hour=hora_h, minute=hora_m, second=0, microsecond=0)
        if alvo <= agora:
            # Horário já passou hoje — próximo disparo é amanhã
            alvo += timedelta(days=1)
        return (alvo - agora).total_seconds()

    def _calcular_proxima_execucao() -> datetime:
        agora = datetime.now()
        alvo = agora.replace(hour=hora_h, minute=hora_m, second=0, microsecond=0)
        if alvo <= agora:
            alvo += timedelta(days=1)
        return alvo

    def executar_agendado():
        # Aguarda até o horário configurado antes de processar
        espera = _segundos_ate_proximo_horario()
        print(f"[AGENDAMENTO {job_id}] Aguardando {int(espera)}s até {hora_str} para iniciar processamento...")

        # Sleep em fatias de 30s para responder ao cancelamento rapidamente
        restante = espera
        while restante > 0:
            time.sleep(min(30, restante))
            restante -= 30

        inicio, fim = _ultimos_30_dias()
        execution_id = str(uuid.uuid4())
        print(f"[AGENDAMENTO {job_id}] Iniciando processamento — período: {inicio} a {fim}")

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

            cfg = RunConfig(
                modo="manual",
                base_dir=_get_data_dir(alias),
                certs_json_path=str(settings.certs_json_path),
                credentials_json_path=str(settings.credentials_json_path),
                cert_aliases=[alias],
                start=inicio,
                end=fim,
                headless=req.headless,
                chunk_days=req.chunk_days,
                consultar_api=req.consultar_api,
                login_type=req.login_type,
                tipo_nota=req.tipo_nota,
            )
            pcfg = ProcessRunConfig(
                **{k: v for k, v in cfg.__dict__.items()},
                execution_id=execution_id,
                processo_id=proc_id,
            )
            Thread(target=run_with_process, args=(pcfg,), daemon=True).start()

    iniciar_agendamento(
        job_id=job_id,
        func=executar_agendado,
        intervalo_segundos=86400,
        descricao=f"Automático diário {hora_str} — últimos 30 dias — {', '.join(req.cert_aliases)}",
        payload=payload,
    )

    proxima = _calcular_proxima_execucao()
    inicio, fim = _ultimos_30_dias()
    return {
        "success": True,
        "job_id": job_id,
        "tipo": "automatico_diario",
        "hora_execucao": hora_str,
        "intervalo_segundos": 86400,
        "descricao": f"Últimos 30 dias corridos, todo dia às {hora_str}",
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
    municipio: Optional[str] = Query(None),
    cnpj_cpf: Optional[str] = Query(None),
    competencia: Optional[str] = Query(None),
    codigo_servico: Optional[str] = Query(None),
    data_tipo: Optional[str] = Query(None),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    somente_divergentes: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    filters = {
        "cert_alias": cert_alias, "status": status, "municipio": municipio,
        "cnpj_cpf": cnpj_cpf, "competencia": competencia,
        "codigo_servico": codigo_servico, "somente_divergentes": somente_divergentes,
        "data_tipo": data_tipo, "data_inicio": data_inicio, "data_fim": data_fim,
    }
    items, total = listar_notas_agrupadas(filters, page=page, page_size=page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


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
        "timestamp": datetime.now().isoformat(),
    }
