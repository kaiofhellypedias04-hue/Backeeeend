from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = PROJECT_ROOT / ".env"

load_dotenv()
if DOTENV_PATH.exists():
    load_dotenv(DOTENV_PATH)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "sim", "on"}


def _resolve_path(value: str | None, fallback: Path) -> Path:
    if not value:
        return fallback.resolve()

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_server_env(app_env: str) -> bool:
    return app_env == "production" or _env_bool("RENDER", default=False)


def _is_tmp_path(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    return normalized == "/tmp" or normalized.startswith("/tmp/") or normalized.endswith(":/tmp") or "/tmp/" in normalized


def _can_prepare_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except PermissionError:
        return False
    except OSError:
        return False


def ensure_json_file(path: Path, default_content: str = "{}") -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_content, encoding="utf-8")


@dataclass(frozen=True)
class AppSettings:
    app_name: str
    app_version: str
    app_env: str
    project_root: Path
    port: int
    app_data_dir: Path
    output_dir: Path
    temp_dir: Path
    certs_dir: Path
    certs_json_path: Path
    credentials_json_path: Path
    secrets_file_path: Path
    playwright_script_path: Path
    package_json_path: Path
    node_bin: str
    npm_bin: str
    playwright_timeout_ms: int
    cors_origins: list[str]
    enable_keyring_fallback: bool
    database_url: str | None
    db_sslmode: str | None
    db_connect_timeout: int
    s3_endpoint: str | None
    s3_bucket: str | None
    s3_access_key: str | None
    s3_secret_key: str | None
    s3_region: str

    def ensure_runtime_dirs(self) -> None:
        directories = {
            self.app_data_dir,
            self.output_dir,
            self.temp_dir,
            self.certs_dir,
            self.certs_json_path.parent,
            self.credentials_json_path.parent,
            self.secrets_file_path.parent,
        }
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        ensure_json_file(self.certs_json_path, "[]")
        ensure_json_file(self.credentials_json_path, "[]")
        ensure_json_file(self.secrets_file_path)

    @property
    def cors_allow_all(self) -> bool:
        return any(origin == "*" for origin in self.cors_origins)

    @property
    def cors_allow_credentials(self) -> bool:
        return not self.cors_allow_all

    def validate(self, require_database: bool = True) -> None:
        missing: list[str] = []
        if require_database and not self.database_url:
            missing.append("DATABASE_URL")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(
                f"Variáveis obrigatórias ausentes: {joined}. "
                "Defina-as nas environment variables do Render ou em um .env local."
            )

    def validate_runtime_paths_for_production(self, raise_on_tmp: bool = False) -> None:
        if self.app_env != "production":
            return

        runtime_paths = {
            "APP_DATA_DIR": self.app_data_dir,
            "OUTPUT_DIR": self.output_dir,
            "TEMP_DIR": self.temp_dir,
            "CERTS_DIR": self.certs_dir,
            "CERTS_JSON_PATH": self.certs_json_path,
            "CREDENTIALS_JSON_PATH": self.credentials_json_path,
            "SECRETS_FILE_PATH": self.secrets_file_path,
        }
        tmp_entries = [f"{name}={path}" for name, path in runtime_paths.items() if _is_tmp_path(path)]
        if not tmp_entries:
            return

        message = (
            "Configuracao de runtime insegura para producao: os caminhos abaixo apontam para /tmp: "
            + ", ".join(tmp_entries)
            + ". Defina APP_DATA_DIR e caminhos derivados para um volume persistente, como /var/data/backend."
        )
        if raise_on_tmp:
            raise RuntimeError(message)
        print(f"[settings] AVISO: {message}")

    def as_runtime_info(self) -> dict[str, str | bool | list[str]]:
        return {
            "app_env": self.app_env,
            "version": self.app_version,
            "project_root": str(self.project_root),
            "app_data_dir": str(self.app_data_dir),
            "output_dir": str(self.output_dir),
            "temp_dir": str(self.temp_dir),
            "certs_dir": str(self.certs_dir),
            "certs_json_path": str(self.certs_json_path),
            "credentials_json_path": str(self.credentials_json_path),
            "secrets_file_path": str(self.secrets_file_path),
            "cors_origins": self.cors_origins,
            "keyring_fallback": self.enable_keyring_fallback,
        }

    @property
    def normalized_database_url(self) -> str | None:
        if not self.database_url:
            return None
        raw = self.database_url.strip()
        if raw.startswith("postgres://"):
            raw = "postgresql://" + raw[len("postgres://") :]

        if not self.db_sslmode:
            return raw

        parts = urlsplit(raw)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.setdefault("sslmode", self.db_sslmode)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    app_env = os.getenv("APP_ENV", "development").strip().lower() or "development"
    default_app_data_dir = Path(os.getenv("APP_DATA_DIR", "/data/backend"))
    app_data_dir = _resolve_path(os.getenv("APP_DATA_DIR"), default_app_data_dir)
    output_dir = app_data_dir / "saida"
    temp_dir = app_data_dir / "temp"
    certs_dir = app_data_dir / "certs"
    certs_json_path = certs_dir / "certs.json"
    credentials_json_path = app_data_dir / "credentials.json"
    secrets_file_path = app_data_dir / "secrets.runtime.json"

    default_cors = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173"
    cors_raw = os.getenv("CORS_ORIGINS")
    cors_origins = _parse_csv(cors_raw if cors_raw is not None else (default_cors if app_env != "production" else ""))

    settings = AppSettings(
        app_name="API Auditoria NFS-e",
        app_version="2.2.0",
        app_env=app_env,
        project_root=PROJECT_ROOT,
        port=int(os.getenv("PORT", "8000")),
        app_data_dir=app_data_dir,
        output_dir=output_dir,
        temp_dir=temp_dir,
        certs_dir=certs_dir,
        certs_json_path=certs_json_path,
        credentials_json_path=credentials_json_path,
        secrets_file_path=secrets_file_path,
        playwright_script_path=_resolve_path(
            os.getenv("PLAYWRIGHT_SCRIPT_PATH"),
            PROJECT_ROOT / "playwright_nfse_download.js",
        ),
        package_json_path=_resolve_path(os.getenv("PACKAGE_JSON_PATH"), PROJECT_ROOT / "package.json"),
        node_bin=os.getenv("NODE_BIN", "node"),
        npm_bin=os.getenv("NPM_BIN", "npm"),
        playwright_timeout_ms=int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "300000")),
        cors_origins=cors_origins,
        enable_keyring_fallback=_env_bool("ENABLE_KEYRING_FALLBACK", default=app_env != "production"),
        database_url=os.getenv("DATABASE_URL"),
        db_sslmode=os.getenv("DB_SSLMODE", "require"),
        db_connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "15")),
        s3_endpoint=os.getenv("S3_ENDPOINT"),
        s3_bucket=os.getenv("S3_BUCKET"),
        s3_access_key=os.getenv("S3_ACCESS_KEY"),
        s3_secret_key=os.getenv("S3_SECRET_KEY"),
        s3_region=os.getenv("S3_REGION", "us-east-1"),
    )
    if not _can_prepare_directory(app_data_dir):
        raise RuntimeError(
            f"APP_DATA_DIR inacessivel: {app_data_dir}. "
            "Configure APP_DATA_DIR para um volume persistente com permissao de escrita."
        )
    settings.ensure_runtime_dirs()
    print(f"[settings] APP_DATA_DIR = {settings.app_data_dir}")
    print(f"[settings] CERTS_DIR = {settings.certs_dir}")
    settings.validate_runtime_paths_for_production(raise_on_tmp=False)
    return settings


BASE_DATA_DIR = _resolve_path(os.getenv("APP_DATA_DIR"), Path(os.getenv("APP_DATA_DIR", "/data/backend")))
APP_DATA_DIR = BASE_DATA_DIR
OUTPUT_DIR = BASE_DATA_DIR / "saida"
TEMP_DIR = BASE_DATA_DIR / "temp"
CERTS_DIR = BASE_DATA_DIR / "certs"
CERTS_JSON_PATH = CERTS_DIR / "certs.json"
CREDENTIALS_JSON_PATH = BASE_DATA_DIR / "credentials.json"
SECRETS_FILE_PATH = BASE_DATA_DIR / "secrets.runtime.json"


def env_names_for_alias(prefix: str, alias: str) -> Iterable[str]:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in alias.upper()).strip("_")
    if normalized:
        yield f"{prefix}_{normalized}"
        yield f"{prefix}__{normalized}"
