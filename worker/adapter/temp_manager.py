"""Temp file management for legacy certs/credentials.json compatibility."""
import json
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple
from ..logging import StructuredLogger
from modules.settings import get_settings

class TempManager:
    @staticmethod
    def create_temp_jsons(
        client_id: str,
        cert_path: Optional[str],
        cert_password: Optional[str],
        cred_username: Optional[str],
        cred_password: Optional[str],
        debug: bool = False
    ) -> Tuple[Optional[Path], Optional[Path], Path]:
        """Create legacy certs.json/credentials.json in unique temp dir."""
        settings = get_settings()
        settings.ensure_runtime_dirs()
        temp_base = settings.temp_dir / f"temp_worker_{int(os.urandom(4).hex(), 16)}"
        temp_base.mkdir(parents=True, exist_ok=True)
        
        certs_path = None
        creds_path = None
        
        # Certs temp (if cert)
        if cert_path:
            certs_data = [{'alias': client_id, 'pfxPath': cert_path}]
            # Note: password NOT in json (keyring legacy)
            certs_path = temp_base / 'certs.json'
            with open(certs_path, 'w', encoding='utf-8') as f:
                json.dump(certs_data, f)
        
        # Creds temp (if cred)
        if cred_username:
            creds_data = [{'alias': client_id, 'cpf_cnpj': cred_username}]
            creds_path = temp_base / 'credentials.json'
            with open(creds_path, 'w', encoding='utf-8') as f:
                json.dump(creds_data, f)
        
        return certs_path, creds_path, temp_base

    @staticmethod
    def cleanup(temp_base: Path, logger: StructuredLogger):
        """Cleanup temp dir."""
        if temp_base.exists():
            shutil.rmtree(temp_base)
            logger.info('Temp files cleaned', {'temp_dir': str(temp_base)})

    @staticmethod
    def get_debug_dir(temp_base: Path) -> Optional[str]:
        """Debug: expose temp_dir path."""
        return str(temp_base) if temp_base.exists() else None

