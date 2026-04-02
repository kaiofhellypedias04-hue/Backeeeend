"""Secrets resolution for certs/credentials by ID."""
from pathlib import Path
from typing import Optional, Tuple

from .schemas import APIInputPayload
from ..models import ErrorCode as LegacyErrorCode
from modules.cert_manager import get_password, get_credential_password
from modules.config_loader import carregar_certificados, carregar_credenciais


class SecretsResolver:
    @staticmethod
    def resolve_certificate(cert_id: str) -> Tuple[Optional[str], Optional[str]]:
        certs = carregar_certificados()
        cert = next((item for item in certs if item.get("alias") == cert_id), None)
        if not cert:
            raise ValueError(LegacyErrorCode.CERTIFICATE_NOT_FOUND.value)

        path = cert.get("pfxPath") or ""
        if not Path(path).exists():
            raise ValueError(LegacyErrorCode.CERTIFICATE_INVALID.value)

        password = get_password(cert_id)
        if not password:
            raise ValueError(LegacyErrorCode.CERTIFICATE_INVALID.value + ": missing password")

        return path, password

    @staticmethod
    def resolve_credential(cred_id: str) -> Tuple[Optional[str], Optional[str]]:
        creds = carregar_credenciais()
        cred = next((item for item in creds if item.get("alias") == cred_id), None)
        if not cred:
            raise ValueError(LegacyErrorCode.CREDENTIAL_NOT_FOUND.value)

        username = cred.get("cpf_cnpj")
        password = get_credential_password(cred_id)
        if not password:
            raise ValueError(LegacyErrorCode.LOGIN_FAILED.value + ": missing credential password")

        return username, password

    @staticmethod
    def resolve(payload: APIInputPayload) -> dict:
        secrets = {}
        if payload.loginType == "certificado" and payload.certificateId:
            secrets["cert_path"], secrets["cert_password"] = SecretsResolver.resolve_certificate(payload.certificateId)
        elif payload.loginType == "cpf_cnpj" and payload.credentialId:
            secrets["cred_username"], secrets["cred_password"] = SecretsResolver.resolve_credential(payload.credentialId)
        return secrets
