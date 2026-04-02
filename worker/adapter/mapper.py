"""Payload mapping: API → Context → Runner."""
from datetime import date, datetime
from typing import Dict, Any
from pathlib import Path
from pydantic import ValidationError
from .schemas import APIInputPayload, ExecutionContext, RunnerPayload
from .secrets_resolver import SecretsResolver
from .temp_manager import TempManager
from ..logging import StructuredLogger
from ...worker.models import ErrorCode as LegacyErrorCode

class Mapper:
    @staticmethod
    def api_to_runner(payload: APIInputPayload, logger: StructuredLogger) -> RunnerPayload:
        """Full mapping with resolution."""
        try:
            # 1. Resolve secrets
            secrets = SecretsResolver.resolve(payload)
            logger.info('Secrets resolved', {'clientId': payload.clientId, 'loginType': payload.loginType})
            
            # 2. Dates
            start = datetime.fromisoformat(payload.startDate).date()
            end = datetime.fromisoformat(payload.endDate).date()
            
            # 3. Temp files (legacy compat)
            certs_path, creds_path, temp_base = TempManager.create_temp_jsons(
                payload.clientId,
                secrets.get('cert_path'),
                secrets.get('cert_password'),
                secrets.get('cred_username'),
                secrets.get('cred_password'),
                payload.debug
            )
            
            # Context (intermediate)
            context = ExecutionContext(
                executionId=payload.executionId,
                clientId=payload.clientId,
                cert_path=secrets.get('cert_path'),
                cert_password=secrets.get('cert_password'),
                cred_username=secrets.get('cred_username'),
                cred_password=secrets.get('cred_password'),
                loginType=payload.loginType,
                temp_certs_path=str(certs_path) if certs_path else None,
                temp_creds_path=str(creds_path) if creds_path else None,
                base_dir=payload.baseDir or str(Path.cwd() / 'data'),
                start=start,
                end=end,
                headless=payload.headless,
                chunk_days=payload.chunkDays,
                tipo_nota=payload.tipoNota,
                debug=payload.debug
            )
            
            # 4. Runner payload
            runner_payload = RunnerPayload(
                base_dir=context.base_dir,
                certs_json_path=context.temp_certs_path or '',
                credentials_json_path=context.temp_creds_path or '',
                cert_aliases=[payload.clientId],
                start=context.start,
                end=context.end,
                headless=context.headless,
                chunk_days=context.chunk_days,
                login_type=context.loginType,
                tipo_nota=context.tipo_nota
            )
            
            # Temp base to executor for cleanup
            runner_payload.temp_base = temp_base  # Temp extension (subproc-ready)
            
            logger.info('Mapping complete', {'executionId': payload.executionId})
            return runner_payload
            
        except ValidationError as e:
            logger.error('Validation failed', {'details': str(e)})
            raise ValueError(LegacyErrorCode.VALIDATION_ERROR.value)
        except ValueError as e:
            logger.error('Resolution error', {'details': str(e)})
            raise

