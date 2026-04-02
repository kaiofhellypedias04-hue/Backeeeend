"""
Integracao com storage S3 compativel.

- Upload de PDFs, XMLs e relatorios apos processamento
- Geracao de URLs pre-assinadas para download
- Limpeza automatica de arquivos antigos

MODO COMPATIVEL SEM S3:
- Se o S3/MinIO nao estiver configurado, o sistema continua operando em modo local
- Uploads retornam o storage_key "logico" para nao quebrar o fluxo do processo
- Downloads continuam podendo usar caminho_local no backend
- Limpeza de storage remoto e ignorada quando S3 nao estiver configurado
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .settings import get_settings

logger = logging.getLogger(__name__)

RETENCAO_DIAS = 15

_settings_cache: Optional[dict] = None
_settings_lock = threading.Lock()
_thread_local = threading.local()


def get_s3_settings() -> dict:
    global _settings_cache
    if _settings_cache is None:
        with _settings_lock:
            if _settings_cache is None:
                settings = get_settings()
                _settings_cache = {
                    "endpoint": settings.s3_endpoint,
                    "bucket": settings.s3_bucket,
                    "access_key": settings.s3_access_key,
                    "secret_key": settings.s3_secret_key,
                    "region": settings.s3_region,
                }
    return _settings_cache


def is_s3_configured() -> bool:
    s = get_s3_settings()
    if not s["endpoint"] or not s["bucket"]:
        return False
    if not s["access_key"] or not s["secret_key"]:
        return False

    settings = get_settings()
    try:
        host = (urlsplit(s["endpoint"]).hostname or "").lower()
    except Exception:
        host = ""

    if settings.app_env == "production" and host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        logger.warning("[S3] Endpoint local ignorado em producao: %s", s["endpoint"])
        return False

    return True


def get_s3_client():
    """Retorna um cliente S3 exclusivo para a thread atual."""
    if not is_s3_configured():
        return None

    if not hasattr(_thread_local, "client"):
        import boto3
        from botocore.client import Config

        s = get_s3_settings()
        _thread_local.client = boto3.client(
            "s3",
            endpoint_url=s["endpoint"],
            aws_access_key_id=s["access_key"],
            aws_secret_access_key=s["secret_key"],
            region_name=s["region"],
            config=Config(
                signature_version="s3v4",
                max_pool_connections=1,
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )
    return _thread_local.client


def upload_file(
    file_path: str,
    storage_key: str,
    content_type: Optional[str] = None,
) -> Optional[str]:
    """
    Faz upload para S3 quando configurado.
    Quando S3 NAO estiver configurado, opera em modo local sem quebrar o fluxo:
    - valida que o arquivo local existe
    - retorna o storage_key logico como sucesso
    """
    path = Path(file_path)

    if not path.exists():
        logger.error("[Storage] Arquivo local nao encontrado: %s", file_path)
        return None

    s3 = get_s3_client()
    if s3 is None:
        logger.warning(
            "[Storage] S3 nao configurado. Mantendo arquivo em modo local: %s -> %s",
            file_path,
            storage_key,
        )
        return storage_key

    bucket = get_s3_settings()["bucket"]

    try:
        data = path.read_bytes()
        kwargs = {"Bucket": bucket, "Key": storage_key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type

        s3.put_object(**kwargs)
        logger.debug("[S3] Upload OK: %s (%.0f KB)", storage_key, len(data) / 1024)
        return storage_key
    except Exception as exc:
        logger.error("[S3] Erro no upload de %s: %s", file_path, exc)
        return None


def upload_xml(file_path: str, storage_key: str) -> Optional[str]:
    return upload_file(file_path, storage_key, "application/xml")


def upload_pdf(file_path: str, storage_key: str) -> Optional[str]:
    return upload_file(file_path, storage_key, "application/pdf")


def upload_relatorio(file_path: str, storage_key: str) -> Optional[str]:
    return upload_file(
        file_path,
        storage_key,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def generate_presigned_download_url(
    storage_key: str,
    expires_in: int = 3600,
) -> Optional[str]:
    s3 = get_s3_client()
    if s3 is None:
        logger.info("[Storage] URL pre-assinada indisponivel em modo local para %s", storage_key)
        return None

    s = get_s3_settings()
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": s["bucket"], "Key": storage_key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        logger.error("[S3] Erro ao gerar URL pre-assinada para %s: %s", storage_key, exc)
        return None


def get_local_file_path(caminho_local: str) -> Path:
    path = Path(caminho_local)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo local nao encontrado: {caminho_local}")
    return path


def limpar_arquivos_antigos_minio(dias: int = RETENCAO_DIAS) -> dict:
    if not is_s3_configured():
        logger.info("[Storage] S3 nao configurado; limpeza remota ignorada.")
        return {"removidos": 0, "erros": 0}

    s3 = get_s3_client()
    if s3 is None:
        logger.info("[Storage] Cliente S3 indisponivel; limpeza remota ignorada.")
        return {"removidos": 0, "erros": 0}

    s = get_s3_settings()
    bucket = s["bucket"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=dias)

    removed = 0
    errors = 0
    removed_keys = []

    logger.info("[S3] Iniciando limpeza de arquivos anteriores a %s", cutoff.strftime("%d/%m/%Y %H:%M UTC"))

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                last_modified = obj["LastModified"]
                if last_modified.tzinfo is None:
                    last_modified = last_modified.replace(tzinfo=timezone.utc)

                if last_modified < cutoff:
                    try:
                        s3.delete_object(Bucket=bucket, Key=obj["Key"])
                        removed_keys.append(obj["Key"])
                        removed += 1
                        logger.debug("[S3] Removido: %s", obj["Key"])
                    except Exception as exc:
                        errors += 1
                        logger.error("[S3] Erro ao remover %s: %s", obj["Key"], exc)
    except Exception as exc:
        logger.error("[S3] Erro ao listar objetos para limpeza: %s", exc)

    if removed_keys:
        try:
            from .db import get_conn

            with get_conn() as conn:
                for key in removed_keys:
                    conn.execute(
                        "UPDATE nfse_processo_arquivos SET storage_key = NULL WHERE storage_key = %s",
                        (key,),
                    )
            logger.info("[S3] Banco atualizado: %s registros marcados sem storage_key", len(removed_keys))
        except Exception as exc:
            logger.warning("[S3] Nao foi possivel atualizar banco apos limpeza: %s", exc)

    logger.info("[S3] Limpeza concluida: %s removidos, %s erros", removed, errors)
    return {"removidos": removed, "erros": errors, "chaves": removed_keys}
