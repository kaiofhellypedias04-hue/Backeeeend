from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Optional

from .settings import ensure_json_file, env_names_for_alias, get_settings


_LOCK = Lock()


def _read_store(path: Path) -> dict:
    ensure_json_file(path)
    if not path.exists():
        return {"certificates": {}, "credentials": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"certificates": {}, "credentials": {}}
    if not isinstance(data, dict):
        return {"certificates": {}, "credentials": {}}
    data.setdefault("certificates", {})
    data.setdefault("credentials", {})
    return data


def _write_store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json_mapping(env_name: str) -> dict[str, str]:
    import os

    raw = os.getenv(env_name)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v is not None}


def _get_from_keyring(service_name: str, alias: str) -> Optional[str]:
    settings = get_settings()
    if settings.app_env == "production" or not settings.enable_keyring_fallback:
        return None
    try:
        import keyring
    except Exception:
        return None
    try:
        return keyring.get_password(service_name, alias)
    except Exception:
        return None


def _set_in_keyring(service_name: str, alias: str, secret: str) -> None:
    settings = get_settings()
    if settings.app_env == "production" or not settings.enable_keyring_fallback:
        return
    try:
        import keyring
        keyring.set_password(service_name, alias, secret)
    except Exception:
        return


def _delete_from_keyring(service_name: str, alias: str) -> None:
    settings = get_settings()
    if settings.app_env == "production" or not settings.enable_keyring_fallback:
        return
    try:
        import keyring
    except Exception:
        return
    try:
        keyring.delete_password(service_name, alias)
    except Exception:
        return


def _get_secret(kind: str, alias: str, service_name: str, per_alias_prefix: str, json_env_name: str) -> Optional[str]:
    import os

    for env_name in env_names_for_alias(per_alias_prefix, alias):
        if os.getenv(env_name):
            return os.getenv(env_name)

    mapped = _read_json_mapping(json_env_name)
    if alias in mapped:
        return mapped[alias]

    settings = get_settings()
    with _LOCK:
        store = _read_store(settings.secrets_file_path)
        secret = store.get(kind, {}).get(alias)
        if secret:
            return secret

    return _get_from_keyring(service_name, alias)


def _set_secret(kind: str, alias: str, secret: str, service_name: str) -> None:
    settings = get_settings()
    settings.ensure_runtime_dirs()
    with _LOCK:
        store = _read_store(settings.secrets_file_path)
        store.setdefault(kind, {})
        store[kind][alias] = secret
        _write_store(settings.secrets_file_path, store)
    _set_in_keyring(service_name, alias, secret)


def _delete_secret(kind: str, alias: str, service_name: str) -> None:
    settings = get_settings()
    with _LOCK:
        store = _read_store(settings.secrets_file_path)
        bucket = store.setdefault(kind, {})
        if alias in bucket:
            del bucket[alias]
            _write_store(settings.secrets_file_path, store)
    _delete_from_keyring(service_name, alias)


def get_certificate_password(alias: str) -> Optional[str]:
    return _get_secret(
        kind="certificates",
        alias=alias,
        service_name="nfse_auditoria",
        per_alias_prefix="CERT_PASSWORD",
        json_env_name="CERT_PASSWORDS_JSON",
    )


def set_certificate_password(alias: str, password: str) -> None:
    _set_secret("certificates", alias, password, "nfse_auditoria")


def delete_certificate_password(alias: str) -> None:
    _delete_secret("certificates", alias, "nfse_auditoria")


def get_credential_password(alias: str) -> Optional[str]:
    return _get_secret(
        kind="credentials",
        alias=alias,
        service_name="nfse_auditoria_credentials",
        per_alias_prefix="CREDENTIAL_PASSWORD",
        json_env_name="CREDENTIAL_PASSWORDS_JSON",
    )


def set_credential_password(alias: str, password: str) -> None:
    _set_secret("credentials", alias, password, "nfse_auditoria_credentials")


def delete_credential_password(alias: str) -> None:
    _delete_secret("credentials", alias, "nfse_auditoria_credentials")
