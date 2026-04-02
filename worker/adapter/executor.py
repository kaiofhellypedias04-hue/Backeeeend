"""Worker executor: adapter orchestration."""
import sys
import importlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from ..models import WorkerPayload, WorkerResult, WorkerStatus, ErrorCode
from ..logging import StructuredLogger
from .schemas import APIInputPayload
from .mapper import Mapper
from .temp_manager import TempManager
from modules.runner import run_processing  # Direct import (v1); future: subprocess

class Executor:
    def __init__(self):
        self.mapper = Mapper()

    def execute(self, input_payload: Dict[str, Any], debug: bool = False) -> Dict[str, Any]:
        """
        Main entry: dict → WorkerResult dict.
        Subproc-ready: no state.
        """
        logger = StructuredLogger('executor')
        
        started = datetime.utcnow()
        execution_id = input_payload.get('executionId', 'unknown')
        logger.info('Execution started', {'executionId': execution_id})

        try:
            # Parse API input
            api_payload = APIInputPayload(**input_payload)
            
            # Map → RunnerPayload
            runner_payload = self.mapper.api_to_runner(api_payload, logger)
            temp_base: Path = runner_payload.temp_base
            
            # Execute runner (Python import v1)
            logger.info('Invoking run_processing', {'executionId': execution_id})
            run_processing(runner_payload)  # Legacy RunConfig kwargs compat
            
            # Success
            result = WorkerResult(
                status=WorkerStatus.COMPLETED,
                executionId=execution_id,
                startedAt=started,
                finishedAt=datetime.utcnow(),
                result={'processed': True},
                logs=logger.get_logs()
            )
            
            if api_payload.debug:
                result.tempDir = TempManager.get_debug_dir(temp_base)
            
            # Cleanup (default)
            if not api_payload.debug:
                TempManager.cleanup(temp_base, logger)
            
            logger.info('Execution completed')
            return result.to_dict()
            
        except Exception as e:
            logger.error('Execution failed', {'error': str(e), 'executionId': execution_id})
            
            # Map to standard error
            error_code = ErrorCode.UNEXPECTED_ERROR
            if 'VALIDATION_ERROR' in str(e):
                error_code = ErrorCode.VALIDATION_ERROR
            elif any(code.value in str(e) for code in [ErrorCode.CERTIFICATE_NOT_FOUND, ErrorCode.LOGIN_FAILED]):
                error_code = ErrorCode.LOGIN_FAILED  # etc. expand
            
            result = WorkerResult(
                status=WorkerStatus.FAILED,
                executionId=execution_id,
                startedAt=started,
                finishedAt=datetime.utcnow(),
                errorCode=error_code,
                errorMessage=str(e),
                logs=logger.get_logs()
            )
            
            # Always cleanup on error
            if 'temp_base' in locals():
                TempManager.cleanup(temp_base, logger)
            
            return result.to_dict()

# Legacy entry compat
def structured_execute(payload_dict: Dict[str, Any]) -> Dict[str, Any]:
    executor = Executor()
    return executor.execute(payload_dict)

