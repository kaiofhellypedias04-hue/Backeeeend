"""
Gerenciamento de certificados (.pfx) e credenciais CPF/CNPJ.

- Certificados novos preferem Supabase Storage quando configurado.
- Certificados legados com pfxPath local continuam funcionando via fallback.
- Senhas usam o cofre/secret store existente; nao sao logadas.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .certificados_repo import (
    listar_certificados as listar_certificados_db,
    remover_certificado as remover_certificado_db,
    upsert_certificado as upsert_certificado_db,
)
from .cert_storage import (
    delete_certificate_object,
    is_supabase_cert_storage_configured,
    upload_certificate_bytes,
)
from .secret_store import (
    delete_certificate_password,
    delete_credential_password as delete_credential_secret,
    get_certificate_password,
    get_credential_password as get_credential_secret,
    set_certificate_password,
    set_credential_password as set_credential_secret,
)
from .settings import ensure_json_file, get_settings


def _apenas_digitos(s: str) -> str:
    return re.sub(r"\D", "", s)


def _validar_cpf(cpf: str) -> bool:
    c = _apenas_digitos(cpf)
    if len(c) != 11 or len(set(c)) == 1:
        return False
    soma = sum(int(c[i]) * (10 - i) for i in range(9))
    d1 = (soma * 10 % 11) % 10
    if d1 != int(c[9]):
        return False
    soma = sum(int(c[i]) * (11 - i) for i in range(10))
    d2 = (soma * 10 % 11) % 10
    return d2 == int(c[10])


def _validar_cnpj(cnpj: str) -> bool:
    c = _apenas_digitos(cnpj)
    if len(c) != 14 or len(set(c)) == 1:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    soma = sum(int(c[i]) * pesos1[i] for i in range(12))
    d1 = 0 if soma % 11 < 2 else 11 - (soma % 11)
    if d1 != int(c[12]):
        return False
    soma = sum(int(c[i]) * pesos2[i] for i in range(13))
    d2 = 0 if soma % 11 < 2 else 11 - (soma % 11)
    return d2 == int(c[13])


def validar_cpf_cnpj(valor: str) -> bool:
    digitos = _apenas_digitos(valor)
    if len(digitos) == 11:
        return _validar_cpf(digitos)
    if len(digitos) == 14:
        return _validar_cnpj(digitos)
    return False


def _safe_alias_filename(alias: str) -> str:
    safe = re.sub(r"[^\w\-. ]", "_", alias.strip()).strip(" ._")
    return safe or "certificado"


def _certs_path() -> str:
    return str(get_settings().certs_json_path)


def _credentials_path() -> str:
    return str(get_settings().credentials_json_path)


def _certs_dir() -> Path:
    settings = get_settings()
    settings.ensure_runtime_dirs()
    settings.certs_dir.mkdir(parents=True, exist_ok=True)
    return settings.certs_dir


def load_certs(certs_json_path: str) -> List[Dict]:
    try:
        certs_db = listar_certificados_db()
        if certs_db:
            return certs_db
    except Exception:
        pass

    ensure_json_file(Path(certs_json_path), "[]")
    if not os.path.exists(certs_json_path):
        return []
    with open(certs_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        alias = (item.get("alias") or "").strip()
        pfx_path = (item.get("pfxPath") or "").strip()
        storage_path = (item.get("storage_path") or "").strip()
        if not alias or (not pfx_path and not storage_path):
            continue

        normalized = {"alias": alias}
        if pfx_path:
            normalized["pfxPath"] = pfx_path
        if item.get("storage_provider"):
            normalized["storage_provider"] = item.get("storage_provider")
        if item.get("storage_bucket"):
            normalized["storage_bucket"] = item.get("storage_bucket")
        if storage_path:
            normalized["storage_path"] = storage_path
        if item.get("original_filename"):
            normalized["original_filename"] = item.get("original_filename")
        out.append(normalized)
    return out


def save_certs(certs_json_path: str, certs: List[Dict]) -> None:
    dir_path = os.path.dirname(certs_json_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    with open(certs_json_path, "w", encoding="utf-8") as f:
        json.dump(certs, f, ensure_ascii=False, indent=2)


def get_password(alias: str) -> Optional[str]:
    return get_certificate_password(alias)


def set_password(alias: str, password: str) -> None:
    set_certificate_password(alias, password)


def delete_password(alias: str) -> None:
    delete_certificate_password(alias)


def upsert_cert(
    certs_json_path: str,
    alias: str,
    pfxPath: str,
    *,
    storage_provider: Optional[str] = None,
    storage_bucket: Optional[str] = None,
    storage_path: Optional[str] = None,
    original_filename: Optional[str] = None,
) -> List[Dict]:
    try:
        upsert_certificado_db(
            alias.strip(),
            pfxPath.strip(),
            storage_provider=storage_provider,
            storage_bucket=storage_bucket,
            storage_path=storage_path,
            original_filename=original_filename,
        )
        return load_certs(certs_json_path)
    except RuntimeError:
        pass

    certs = load_certs(certs_json_path)
    alias_n = alias.strip()
    pfx_n = pfxPath.strip()
    updated = False

    for c in certs:
        if c.get("alias") == alias_n:
            c["pfxPath"] = pfx_n
            if storage_provider:
                c["storage_provider"] = storage_provider
            else:
                c.pop("storage_provider", None)
            if storage_bucket:
                c["storage_bucket"] = storage_bucket
            else:
                c.pop("storage_bucket", None)
            if storage_path:
                c["storage_path"] = storage_path
            else:
                c.pop("storage_path", None)
            if original_filename:
                c["original_filename"] = original_filename
            else:
                c.pop("original_filename", None)
            updated = True
            break

    if not updated:
        item = {"alias": alias_n, "pfxPath": pfx_n}
        if storage_provider:
            item["storage_provider"] = storage_provider
        if storage_bucket:
            item["storage_bucket"] = storage_bucket
        if storage_path:
            item["storage_path"] = storage_path
        if original_filename:
            item["original_filename"] = original_filename
        certs.append(item)

    save_certs(certs_json_path, certs)
    return certs


def remove_cert(certs_json_path: str, alias: str) -> List[Dict]:
    try:
        remover_certificado_db(alias)
        return load_certs(certs_json_path)
    except RuntimeError:
        pass

    certs = [c for c in load_certs(certs_json_path) if c.get("alias") != alias]
    save_certs(certs_json_path, certs)
    return certs


def load_credentials(credentials_json_path: str) -> List[Dict]:
    ensure_json_file(Path(credentials_json_path), "[]")
    if not os.path.exists(credentials_json_path):
        return []
    try:
        with open(credentials_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            alias = (item.get("alias") or "").strip()
            cpf_cnpj = (item.get("cpf_cnpj") or "").strip()
            if not alias or not cpf_cnpj:
                continue
            out.append({"alias": alias, "cpf_cnpj": cpf_cnpj})
        return out
    except Exception:
        return []


def save_credentials(credentials_json_path: str, creds: List[Dict]) -> None:
    dir_path = os.path.dirname(credentials_json_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    with open(credentials_json_path, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)


def get_credential_password(alias: str) -> Optional[str]:
    return get_credential_secret(alias)


def set_credential_password(alias: str, password: str) -> None:
    set_credential_secret(alias, password)


def delete_credential_password(alias: str) -> None:
    delete_credential_secret(alias)


def upsert_credential(credentials_json_path: str, alias: str, cpf_cnpj: str) -> List[Dict]:
    creds = load_credentials(credentials_json_path)
    alias_n = alias.strip()
    cpf_cnpj_n = cpf_cnpj.strip()
    updated = False
    for c in creds:
        if c.get("alias") == alias_n:
            c["cpf_cnpj"] = cpf_cnpj_n
            updated = True
            break
    if not updated:
        creds.append({"alias": alias_n, "cpf_cnpj": cpf_cnpj_n})
    save_credentials(credentials_json_path, creds)
    return creds


def remove_credential(credentials_json_path: str, alias: str) -> List[Dict]:
    creds = [c for c in load_credentials(credentials_json_path) if c.get("alias") != alias]
    save_credentials(credentials_json_path, creds)
    return creds


def adicionar_certificado(
    alias: str,
    client_name: str,
    pfx_bytes: bytes,
    password: str,
    original_filename: Optional[str] = None,
) -> dict:
    certs_json = _certs_path()

    if not pfx_bytes:
        raise ValueError("Arquivo de certificado vazio.")

    certs = load_certs(certs_json)
    existing = next((c for c in certs if c.get("alias") == alias), None)

    if is_supabase_cert_storage_configured():
        uploaded = upload_certificate_bytes(alias, pfx_bytes, original_filename)
        upsert_cert(
            certs_json,
            alias,
            uploaded["pfxPath"],
            storage_provider=uploaded["storage_provider"],
            storage_bucket=uploaded["storage_bucket"],
            storage_path=uploaded["storage_path"],
            original_filename=uploaded["original_filename"],
        )
        if existing and existing.get("storage_provider") == "supabase" and existing.get("storage_bucket") and existing.get("storage_path"):
            try:
                delete_certificate_object(existing["storage_bucket"], existing["storage_path"])
            except Exception:
                # Toleramos lixo remoto temporario para nao quebrar o recadastro.
                pass
        elif existing:
            old_path = Path(existing.get("pfxPath", ""))
            if old_path.exists():
                old_path.unlink(missing_ok=True)
        pfx_display = uploaded["pfxPath"]
    else:
        cert_file = _certs_dir() / f"{_safe_alias_filename(alias)}.pfx"
        try:
            cert_file.write_bytes(pfx_bytes)
        except Exception as exc:
            raise RuntimeError(f"Nao foi possivel salvar o certificado em {cert_file}: {exc}") from exc
        upsert_cert(
            certs_json,
            alias,
            str(cert_file),
            original_filename=Path(original_filename or cert_file.name).name,
        )
        pfx_display = str(cert_file)

    set_password(alias, password)
    return {
        "alias": alias,
        "client_name": client_name,
        "pfxPath": pfx_display,
    }


def editar_certificado(alias: str, novo_alias: Optional[str] = None, client_name: Optional[str] = None) -> dict:
    certs_json = _certs_path()
    certs = load_certs(certs_json)

    cert = next((c for c in certs if c.get("alias") == alias), None)
    if not cert:
        raise ValueError(f"Certificado '{alias}' nao encontrado")

    if novo_alias and novo_alias != alias:
        if any(c.get("alias") == novo_alias for c in certs):
            raise ValueError(f"Alias '{novo_alias}' ja esta em uso")

        new_pfx_path = cert.get("pfxPath", "")
        if not cert.get("storage_path"):
            old_path = Path(cert.get("pfxPath", ""))
            new_path = _certs_dir() / f"{_safe_alias_filename(novo_alias)}.pfx"
            if old_path.exists():
                old_path.rename(new_path)
            new_pfx_path = str(new_path)

        senha = get_password(alias)
        if senha:
            set_password(novo_alias, senha)
            delete_password(alias)

        remove_cert(certs_json, alias)
        upsert_cert(
            certs_json,
            novo_alias,
            new_pfx_path,
            storage_provider=cert.get("storage_provider"),
            storage_bucket=cert.get("storage_bucket"),
            storage_path=cert.get("storage_path"),
            original_filename=cert.get("original_filename"),
        )
        return {"alias": novo_alias, "pfxPath": new_pfx_path}

    return {"alias": alias, "pfxPath": cert.get("pfxPath", "")}


def redefinir_senha_certificado(alias: str, nova_senha: str) -> bool:
    certs_json = _certs_path()
    certs = load_certs(certs_json)
    if not any(c.get("alias") == alias for c in certs):
        raise ValueError(f"Certificado '{alias}' nao encontrado")
    set_password(alias, nova_senha)
    return True


def excluir_certificado(alias: str, remover_arquivo: bool = True) -> bool:
    certs_json = _certs_path()
    certs = load_certs(certs_json)
    cert = next((c for c in certs if c.get("alias") == alias), None)
    if not cert:
        raise ValueError(f"Certificado '{alias}' nao encontrado")

    if remover_arquivo:
        if cert.get("storage_provider") == "supabase" and cert.get("storage_bucket") and cert.get("storage_path"):
            delete_certificate_object(cert["storage_bucket"], cert["storage_path"])
        else:
            pfx = Path(cert.get("pfxPath", ""))
            if pfx.exists():
                pfx.unlink(missing_ok=True)

    remove_cert(certs_json, alias)
    delete_password(alias)
    return True


def adicionar_credencial(alias: str, cpf_cnpj: str, password: str) -> dict:
    if not validar_cpf_cnpj(cpf_cnpj):
        raise ValueError(
            f"CPF/CNPJ invalido: '{cpf_cnpj}'. "
            "Informe um CPF (11 digitos) ou CNPJ (14 digitos) valido."
        )

    credentials_json = _credentials_path()
    upsert_credential(credentials_json, alias, cpf_cnpj)
    set_credential_password(alias, password)
    return {"alias": alias, "cpf_cnpj": cpf_cnpj}


def editar_credencial(alias: str, novo_alias: Optional[str] = None, cpf_cnpj: Optional[str] = None) -> dict:
    credentials_json = _credentials_path()
    creds = load_credentials(credentials_json)

    cred = next((c for c in creds if c.get("alias") == alias), None)
    if not cred:
        raise ValueError(f"Credencial '{alias}' nao encontrada")

    if cpf_cnpj and not validar_cpf_cnpj(cpf_cnpj):
        raise ValueError(f"CPF/CNPJ invalido: '{cpf_cnpj}'")

    novo_cpf = cpf_cnpj or cred["cpf_cnpj"]
    destino_alias = (novo_alias or alias).strip()

    if destino_alias != alias:
        if any(c.get("alias") == destino_alias for c in creds):
            raise ValueError(f"Alias '{destino_alias}' ja esta em uso")
        senha = get_credential_password(alias)
        if senha:
            set_credential_password(destino_alias, senha)
            delete_credential_password(alias)
        remove_credential(credentials_json, alias)

    upsert_credential(credentials_json, destino_alias, novo_cpf)
    return {"alias": destino_alias, "cpf_cnpj": novo_cpf}


def redefinir_senha_credencial(alias: str, nova_senha: str) -> bool:
    credentials_json = _credentials_path()
    creds = load_credentials(credentials_json)
    if not any(c.get("alias") == alias for c in creds):
        raise ValueError(f"Credencial '{alias}' nao encontrada")
    set_credential_password(alias, nova_senha)
    return True


def excluir_credencial(alias: str) -> bool:
    credentials_json = _credentials_path()
    creds = load_credentials(credentials_json)
    if not any(c.get("alias") == alias for c in creds):
        raise ValueError(f"Credencial '{alias}' nao encontrada")
    remove_credential(credentials_json, alias)
    delete_credential_password(alias)
    return True
