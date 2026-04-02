"""Pydantic schemas for worker adapter layer."""
from datetime import date
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, validator, Field
from ...worker.models import ErrorCode, WorkerStatus, WorkerResult

class APIInputPayload(BaseModel):
    executionId: str
    clientId: str  # Legacy alias compat
    certificateId: Optional[str] = Field(None, description="Certificate DB ID (req: loginType=certificado)")
    credentialId: Optional[str] = Field(None, description="Credential DB ID (req: loginType=cpf_cnpj)")
    startDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    endDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    headless: bool = Field(True)
    tipoNota: str = Field("tomados")
    loginType: str = Field("certificado")
    baseDir: Optional[str] = Field(None)
    chunkDays: Optional[int] = Field(30)
    debug: bool = Field(False)
    executionConfig: Optional[Dict[str, Any]] = Field({})

    @validator('startDate', 'endDate')
    def validate_dates(cls, v):
        from datetime import datetime
        dt = datetime.fromisoformat(v)
        if dt.date() > date.today():
            raise ValueError('Dates cannot be in future')
        return v

    @validator('loginType')
    def validate_login_type(cls, v, values):
        cert_id = values.get('certificateId')
        cred_id = values.get('credentialId')
        if v == 'certificado' and not cert_id:
            raise ValueError(ErrorCode.CERTIFICATE_NOT_FOUND.value + ': required for certificado')
        if v == 'cpf_cnpj' and not cred_id:
            raise ValueError(ErrorCode.CREDENTIAL_NOT_FOUND.value + ': required for cpf_cnpj')
        return v

class ExecutionContext(BaseModel):
    """Resolved secrets/temp after mapper."""
    executionId: str
    clientId: str
    cert_path: Optional[str] = None
    cert_password: Optional[str] = None
    cred_username: Optional[str] = None
    cred_password: Optional[str] = None
    loginType: str
    temp_certs_path: Optional[str] = None
    temp_creds_path: Optional[str] = None
    base_dir: str
    start: date
    end: date
    headless: bool
    chunk_days: int
    tipo_nota: str
    debug: bool

class RunnerPayload(BaseModel):
    """Direct RunConfig mapping (legacy compat)."""
    modo: str = 'automatico'
    base_dir: str
    certs_json_path: str
    credentials_json_path: str
    cert_aliases: List[str]
    start: date
    end: date
    headless: bool
    chunk_days: int
    consultar_api: bool = True
    login_type: str
    tipo_nota: str

# Type aliases
WorkerResultT = WorkerResult

