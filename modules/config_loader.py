from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .settings import ensure_json_file, get_settings


def _load_json_array(path: Path, label: str) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Erro ao ler {label} em {path}: {exc}")
        return []

    if not isinstance(data, list):
        print(f"{label} inválido em {path}: esperado um array JSON.")
        return []
    return [item for item in data if isinstance(item, dict)]


def carregar_certificados(caminho: str | None = None) -> list[dict]:
    settings = get_settings()
    path = Path(caminho) if caminho else settings.certs_json_path
    legacy_path = settings.project_root / "certs.json"
    if not path.exists() and legacy_path.exists():
        path = legacy_path
    if path == settings.certs_json_path:
        ensure_json_file(path, "[]")
    data = _load_json_array(path, "certs.json")
    certs: list[dict] = []
    for item in data:
        alias = (item.get("alias") or "").strip()
        pfx_path = (item.get("pfxPath") or "").strip()
        if not alias or not pfx_path:
            continue
        certs.append({"alias": alias, "pfxPath": pfx_path})
    return certs


def carregar_credenciais(caminho: str | None = None) -> list[dict]:
    settings = get_settings()
    path = Path(caminho) if caminho else settings.credentials_json_path
    legacy_path = settings.project_root / "credentials.json"
    if not path.exists() and legacy_path.exists():
        path = legacy_path
    if path == settings.credentials_json_path:
        ensure_json_file(path, "[]")
    data = _load_json_array(path, "credentials.json")
    creds: list[dict] = []
    for item in data:
        alias = (item.get("alias") or "").strip()
        cpf_cnpj = (item.get("cpf_cnpj") or "").strip()
        if not alias or not cpf_cnpj:
            continue
        creds.append({"alias": alias, "cpf_cnpj": cpf_cnpj})
    return creds


def listar_aliases(login_type: str) -> set[str]:
    if login_type == "cpf_cnpj":
        return {item["alias"] for item in carregar_credenciais() if item.get("alias")}
    return {item["alias"] for item in carregar_certificados() if item.get("alias")}
