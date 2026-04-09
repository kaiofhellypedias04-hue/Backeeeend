from __future__ import annotations

import json
import logging
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import quote

import requests

from .settings import get_settings

logger = logging.getLogger(__name__)

SUPABASE_PROVIDER = "supabase"


def _safe_alias_segment(alias: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in (alias or "").strip())
    safe = safe.strip("._-")
    return safe or "certificado"


def _logical_pfx_path(bucket: str, storage_path: str) -> str:
    return f"supabase://{bucket}/{storage_path}"


def _guess_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".pfx", ".p12"}:
        return suffix
    return ".pfx"


def is_supabase_cert_storage_configured() -> bool:
    settings = get_settings()
    return bool(settings.supabase_url and settings.supabase_service_role_key and settings.supabase_cert_bucket)


def _storage_headers() -> dict[str, str]:
    settings = get_settings()
    if not settings.supabase_service_role_key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY nao configurada para storage de certificados.")
    return {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
    }


def _storage_url(bucket: str, storage_path: str) -> str:
    settings = get_settings()
    if not settings.supabase_url:
        raise RuntimeError("SUPABASE_URL nao configurada para storage de certificados.")
    object_path = quote(storage_path.lstrip("/"), safe="/")
    return f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_path}"


def upload_certificate_bytes(alias: str, payload: bytes, original_filename: str | None = None) -> dict[str, str]:
    settings = get_settings()
    if not payload:
        raise ValueError("Arquivo de certificado vazio.")
    if not is_supabase_cert_storage_configured():
        raise RuntimeError("Storage Supabase de certificados nao configurado.")

    suffix = _guess_suffix(original_filename)
    storage_path = f"certificados/{_safe_alias_segment(alias)}/{uuid.uuid4().hex}{suffix}"
    response = requests.post(
        _storage_url(settings.supabase_cert_bucket, storage_path),
        headers={
            **_storage_headers(),
            "Content-Type": "application/octet-stream",
            "x-upsert": "true",
        },
        data=payload,
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Falha ao enviar certificado para Supabase Storage: {response.status_code} {response.text[:200]}"
        )

    return {
        "storage_provider": SUPABASE_PROVIDER,
        "storage_bucket": settings.supabase_cert_bucket,
        "storage_path": storage_path,
        "pfxPath": _logical_pfx_path(settings.supabase_cert_bucket, storage_path),
        "original_filename": Path(original_filename or f"{alias}{suffix}").name,
    }


def download_certificate_bytes(storage_bucket: str, storage_path: str) -> bytes:
    response = requests.get(
        _storage_url(storage_bucket, storage_path),
        headers=_storage_headers(),
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Falha ao baixar certificado do Supabase Storage: {response.status_code} {response.text[:200]}"
        )
    return response.content


def delete_certificate_object(storage_bucket: str, storage_path: str) -> None:
    response = requests.delete(
        _storage_url(storage_bucket, storage_path),
        headers=_storage_headers(),
        timeout=30,
    )
    if response.status_code in {200, 204, 404}:
        return
    raise RuntimeError(
        f"Falha ao excluir certificado do Supabase Storage: {response.status_code} {response.text[:200]}"
    )


def certificate_display_name(cert: dict) -> str:
    original = (cert.get("original_filename") or "").strip()
    if original:
        return Path(original).name
    pfx_path = (cert.get("pfxPath") or "").strip()
    storage_path = (cert.get("storage_path") or "").strip()
    if storage_path:
        return Path(storage_path).name
    if pfx_path:
        return Path(pfx_path).name
    return f"{cert.get('alias') or 'certificado'}.pfx"


@contextmanager
def materialize_certificates_for_runtime(
    certs: list[dict],
    aliases: Optional[list[str]] = None,
) -> Iterator[str]:
    """
    Gera um certs.json temporario com pfxPath local para manter compatibilidade
    com o fluxo legado do Playwright.
    """
    settings = get_settings()
    selected_aliases = set(aliases or [])
    with tempfile.TemporaryDirectory(prefix="cert_runtime_", dir=str(settings.temp_dir)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        runtime_entries: list[dict] = []

        for cert in certs:
            alias = (cert.get("alias") or "").strip()
            if not alias:
                continue
            if selected_aliases and alias not in selected_aliases:
                continue

            runtime_entry = dict(cert)
            storage_provider = (cert.get("storage_provider") or "").strip().lower()
            storage_bucket = (cert.get("storage_bucket") or "").strip()
            storage_path = (cert.get("storage_path") or "").strip()

            if storage_provider == SUPABASE_PROVIDER and storage_bucket and storage_path:
                payload = download_certificate_bytes(storage_bucket, storage_path)
                suffix = _guess_suffix(cert.get("original_filename") or storage_path)
                cert_file = tmp_path / f"{_safe_alias_segment(alias)}{suffix}"
                cert_file.write_bytes(payload)
                runtime_entry["pfxPath"] = str(cert_file)
                logger.info("[CertStorage] Certificado '%s' materializado temporariamente para execucao.", alias)
            else:
                # Compatibilidade legada: certificados antigos ainda apontando para arquivo local.
                local_path = (cert.get("pfxPath") or "").strip()
                if not local_path:
                    raise RuntimeError(f"Certificado '{alias}' sem storage_path e sem pfxPath legado.")
                runtime_entry["pfxPath"] = local_path

            runtime_entries.append(runtime_entry)

        certs_json_path = tmp_path / "certs.runtime.json"
        certs_json_path.write_text(json.dumps(runtime_entries, ensure_ascii=False, indent=2), encoding="utf-8")
        yield str(certs_json_path)
