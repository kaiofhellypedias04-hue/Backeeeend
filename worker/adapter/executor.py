"""Worker executor: adapter orchestration via global dispatch queue."""
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from ..models import WorkerResult, WorkerStatus, ErrorCode
from ..logging import StructuredLogger
from .schemas import APIInputPayload
from modules.dispatch_queue import enqueue_dispatch_item
from modules.execucoes_repo import criar_execucao
from modules.processos_repo import criar_processo, obter_processo
from modules.schemas import ProcessoCreate, StatusEnum, normalize_login_type
from modules.settings import get_settings
from modules.timezone_utils import now_utc


def _worker_base_dir(alias: str) -> str:
    settings = get_settings()
    safe = re.sub(r"[^\w\-. ]", "_", str(alias or "").strip()).strip() or "cliente"
    return str(Path(settings.output_dir) / safe)


def _queue_payload_from_worker(api_payload: APIInputPayload) -> dict[str, Any]:
    return {
        "modo": "manual",
        "base_dir": api_payload.baseDir or _worker_base_dir(api_payload.clientId),
        "certs_json_path": str(get_settings().certs_json_path),
        "credentials_json_path": str(get_settings().credentials_json_path),
        "cert_alias": api_payload.clientId,
        "start": api_payload.startDate,
        "end": api_payload.endDate,
        "headless": api_payload.headless,
        "use_chunk_days": bool(api_payload.useChunkDays),
        "chunk_days": api_payload.chunkDays or 30,
        "consultar_api": True,
        "login_type": normalize_login_type(api_payload.loginType),
        "tipo_nota": api_payload.tipoNota,
    }


class Executor:
    def __init__(self):
        pass

    def execute(self, input_payload: Dict[str, Any], debug: bool = False) -> Dict[str, Any]:
        logger = StructuredLogger("executor")
        started = now_utc()
        execution_id = input_payload.get("executionId", "unknown")
        logger.info("Execution started", {"executionId": execution_id})

        try:
            api_payload = APIInputPayload(**input_payload)
            start = datetime.fromisoformat(api_payload.startDate).date()
            end = datetime.fromisoformat(api_payload.endDate).date()

            proc_id = criar_processo(
                ProcessoCreate(
                    execution_id=api_payload.executionId,
                    cert_alias=api_payload.clientId,
                    login_type=api_payload.loginType,
                    tipo_nota=api_payload.tipoNota,
                    start_date=start,
                    end_date=end,
                )
            )
            criar_execucao(api_payload.executionId, proc_id, input_payload)
            enqueue_dispatch_item(
                job_id=api_payload.executionId,
                processo_id=proc_id,
                cert_alias=api_payload.clientId,
                payload_json=_queue_payload_from_worker(api_payload),
            )
            logger.info(
                "Execution enqueued",
                {"executionId": execution_id, "processoId": proc_id, "clientId": api_payload.clientId},
            )

            while True:
                proc = obter_processo(proc_id)
                if proc and proc.status in {StatusEnum.completed, StatusEnum.failed}:
                    break
                time.sleep(5)

            if proc and proc.status == StatusEnum.completed:
                result = WorkerResult(
                    status=WorkerStatus.COMPLETED,
                    executionId=execution_id,
                    startedAt=started,
                    finishedAt=now_utc(),
                    result={"processed": True, "processo_id": proc_id},
                    logs=logger.get_logs(),
                )
                logger.info("Execution completed", {"executionId": execution_id, "processoId": proc_id})
                return result.to_dict()

            error_message = proc.error_message if proc else "processo_nao_encontrado"
            result = WorkerResult(
                status=WorkerStatus.FAILED,
                executionId=execution_id,
                startedAt=started,
                finishedAt=now_utc(),
                errorCode=ErrorCode.PROCESSING_ERROR,
                errorMessage=error_message,
                logs=logger.get_logs(),
            )
            logger.error("Execution failed", {"error": error_message, "executionId": execution_id})
            return result.to_dict()

        except Exception as e:
            logger.error("Execution failed", {"error": str(e), "executionId": execution_id})
            result = WorkerResult(
                status=WorkerStatus.FAILED,
                executionId=execution_id,
                startedAt=started,
                finishedAt=now_utc(),
                errorCode=ErrorCode.UNEXPECTED_ERROR,
                errorMessage=str(e),
                logs=logger.get_logs(),
            )
            return result.to_dict()


def structured_execute(payload_dict: Dict[str, Any]) -> Dict[str, Any]:
    executor = Executor()
    return executor.execute(payload_dict)
