import json
import time
from datetime import datetime
from typing import Dict, Any, List
from pathlib import Path

MAX_LOG_CHARS = 10000

class StructuredLogger:
    def __init__(self, name: str = 'worker', level: str = 'INFO'):
        self.name = name
        self.level = level
        self.logs: List[Dict[str, Any]] = []
        self._file_handler = None

    def _truncate(self, msg: str) -> str:
        if len(msg) > MAX_LOG_CHARS:
            return msg[:MAX_LOG_CHARS] + f"... [truncated {len(msg) - MAX_LOG_CHARS} chars]"
        return msg

    def info(self, message: str, extra: Dict[str, Any] = None):
        self._log('INFO', message, extra)

    def warning(self, message: str, extra: Dict[str, Any] = None):
        self._log('WARNING', message, extra)

    def error(self, message: str, extra: Dict[str, Any] = None):
        self._log('ERROR', message, extra)

    def _log(self, level: str, message: str, extra: Dict[str, Any] = None):
        if level < self.level:
            return

        record = {
            'timestamp': datetime.utcnow().isoformat(),
            'name': self.name,
            'level': level,
            'message': self._truncate(message),
        }
        if extra:
            record['extra'] = extra

        self.logs.append(record)
        print(json.dumps(record, default=str))  # Stdout for CLI

        # Optional file
        if not self._file_handler:
            log_file = Path.cwd() / f"worker_{int(time.time())}.log"
            self._file_handler = open(log_file, 'a')
        self._file_handler.write(json.dumps(record, default=str) + '\n')
        self._file_handler.flush()

    def get_logs(self) -> List[Dict[str, Any]]:
        return self.logs.copy()

    def close(self):
        if self._file_handler:
            self._file_handler.close()


# Legacy compat (remove after)
def structured_run(*args, **kwargs):
    from .models import ErrorCode
    raise NotImplementedError("Refactored to executor; use from adapter.executor")

