from dataclasses import dataclass, asdict, field
from datetime import datetime, date
from typing import Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, validator, Field

class WorkerStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CERTIFICATE_NOT_FOUND = "CERTIFICATE_NOT_FOUND"
    CERTIFICATE_INVALID = "CERTIFICATE_INVALID"
    CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND"
    LOGIN_FAILED = "LOGIN_FAILED"
    PLAYWRIGHT_ERROR = "PLAYWRIGHT_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    PROCESSING_ERROR = "PROCESSING_ERROR"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"

# Legacy dataclass (backwards compat)
@dataclass
class WorkerPayload:
    executionId: str
    clientId: str
    startDate: str  # YYYY-MM-DD
    endDate: str    # YYYY-MM-DD
    certificatePath: Optional[str] = None
    certificatePassword: Optional[str] = None
    credentialUsername: Optional[str] = None
    credentialPassword: Optional[str] = None
    headless: bool = True
    tipoNota: str = "tomados"
    loginType: str = "certificado"
    baseDir: Optional[str] = None
    chunkDays: Optional[int] = 30
    executionConfig: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkerPayload':
        return cls(**data)

@dataclass
class WorkerResult:
    status: WorkerStatus
    executionId: str
    startedAt: datetime
    finishedAt: datetime
    result: Optional[Dict[str, Any]] = None
    errorCode: Optional[ErrorCode] = None
    errorMessage: Optional[str] = None
    logs: List[Dict[str, Any]] = field(default_factory=list)
    tempDir: Optional[str] = None  # Debug only

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

# New Pydantic schemas for adapter
class APIInputPayload(BaseModel):
    executionId: str = Field(..., description="Unique execution ID")
    clientId: str = Field(..., description="Legacy alias")
    certificateId: Optional[str] = Field(None, description="DB ID for cert (req if loginType=certificado)")
    credentialId: Optional[str] = Field(None, description="DB ID for cred (req if loginType=cpf_cnpj)")
    startDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    endDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    headless: bool = True
    tipoNota: str = Field("tomados", regex="^(tomados|prestados)$")
    loginType: str = Field("certificado", regex="^(certificado|cpf_cnpj)$")
    baseDir: Optional[str] = None
    chunkDays: Optional[int] = 30
    debug: bool = False
    executionConfig: Optional[Dict[str, Any]] = {}

    @validator('loginType')
    def validate_login_requirements(cls, v, values):
        if v == 'certificado' and not values.get('certificateId'):
            raise ValueError('certificateId required for loginType=certificado')
        if v == 'cpf_cnpj' and not values.get('credentialId'):
            raise ValueError('credentialId required for loginType=cpf_cnpj')
        return v

class ExecutionContext(BaseModel):
    executionId: str
    clientId: str
    cert_path: Optional[str]
    cert_password: Optional[str]
    cred_username: Optional[str]
    cred_password: Optional[str]
    loginType: str
    # ... other resolved

class RunnerPayload(BaseModel):
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

