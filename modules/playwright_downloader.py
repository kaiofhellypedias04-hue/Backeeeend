from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from shutil import which

from .cert_manager import get_password, get_credential_password
from .settings import get_settings

logger = logging.getLogger(__name__)

EXTERNAL_RETRY_WAIT_SECONDS = (30, 60, 75, 90)
MAX_EXTERNAL_ATTEMPTS = len(EXTERNAL_RETRY_WAIT_SECONDS) + 1


def _normalize_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _extract_last_stage(stdout: str, stderr: str) -> str | None:
    for text in (stderr, stdout):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for line in reversed(lines):
            if line.startswith("[STAGE] "):
                return line[len("[STAGE] ") :].strip()
    return None


def _build_log_tail(stdout: str, stderr: str, limit: int = 8) -> str:
    combined: list[str] = []
    for prefix, text in (("stderr", stderr), ("stdout", stdout)):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for line in lines[-limit:]:
            combined.append(f"{prefix}: {line}")
    if not combined:
        return ""
    tail = combined[-limit:]
    return " | ".join(tail)


def _summarize_playwright_context(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    last_stage = _extract_last_stage(stdout, stderr)
    if last_stage:
        parts.append(f"ultima etapa: {last_stage}")
    tail = _build_log_tail(stdout, stderr)
    if tail:
        parts.append(f"ultimos logs: {tail}")
    return " | ".join(parts)


def _fallback_retryable_error(error_msg: str) -> bool:
    msg = str(error_msg or "").lower()
    transient_markers = (
        "playwright excedeu o timeout",
        "erro executando subprocess",
        "econnreset",
        "connection reset",
        "browser disconnected",
        "target page, context or browser has been closed",
        "net::err",
        "timed out",
        "timeout",
    )
    terminal_markers = (
        "falha no login",
        "máximo de tentativas de re-login atingido",
        "maximo de tentativas de re-login atingido",
        "erro de certificado",
        "senha não configurada",
        "senha nao configurada",
        "credencial não encontrada",
        "credencial nao encontrada",
        "certificado não encontrado",
        "certificado nao encontrado",
        "playwright instalado sem browsers",
        "browser do playwright nao encontrado",
    )
    if any(marker in msg for marker in terminal_markers):
        return False
    return any(marker in msg for marker in transient_markers)


def _should_retry_externally(result: Dict[str, Any], error_msg: str, attempt: int) -> bool:
    if attempt >= MAX_EXTERNAL_ATTEMPTS:
        return False

    if result.get("retryableExternally") is True:
        return True
    if result.get("retryableExternally") is False:
        return False

    error_category = str(result.get("errorCategory") or "").strip().lower()
    if error_category in {"auth_terminal", "session_recovery_exhausted"}:
        return False
    if error_category in {"transient_process", "process_timeout"}:
        return True

    return _fallback_retryable_error(error_msg)


def _preflight_playwright_runtime(settings) -> Optional[str]:
    if which(settings.node_bin) is None:
        return f"Node.js nao encontrado no PATH. Binario configurado: {settings.node_bin}"

    playwright_pkg = settings.package_json_path.parent / "node_modules" / "playwright"
    playwright_core_pkg = settings.package_json_path.parent / "node_modules" / "playwright-core"
    if not playwright_pkg.exists() and not playwright_core_pkg.exists():
        return (
            "Dependencias Node/Playwright nao encontradas no servidor. "
            "Execute npm ci e playwright install no build do Render ou use o Dockerfile do projeto."
        )

    return None


def _validate_playwright_browser_launch(settings) -> Optional[str]:
    validation_script = (
        "const { chromium } = require('playwright');"
        "(async()=>{"
        "const browser = await chromium.launch({ headless: true });"
        "await browser.close();"
        "console.log('PLAYWRIGHT_CHROMIUM_OK');"
        "})().catch(err=>{"
        "console.error(err && (err.stack || err.message) ? (err.stack || err.message) : String(err));"
        "process.exit(1);"
        "});"
    )

    try:
        proc = subprocess.run(
            [settings.node_bin, "-e", validation_script],
            cwd=str(settings.package_json_path.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=min(30, max(10, int(settings.playwright_timeout_ms / 1000))),
        )
    except FileNotFoundError:
        return f"Node.js nao encontrado no PATH. Binario configurado: {settings.node_bin}"
    except subprocess.TimeoutExpired:
        return (
            "Falha ao validar o Chromium do Playwright no ambiente. "
            "A validacao excedeu o tempo limite."
        )
    except Exception as exc:
        return f"Erro ao validar o Chromium do Playwright: {exc}"

    if proc.returncode == 0:
        logger.info("Preflight do Playwright concluido com sucesso.")
        return None

    combined_output = " ".join(
        part.strip() for part in [proc.stderr or "", proc.stdout or ""] if part.strip()
    )
    msg_lower = combined_output.lower()
    if (
        "please run the following command to download new browsers" in msg_lower
        or "executable doesn't exist" in msg_lower
        or "browsertype.launch" in msg_lower
    ):
        return (
            "Playwright/Chromium não instalado no ambiente de deploy. "
            "Configure o build com: npx playwright install --with-deps chromium"
        )

    short_output = combined_output[:500] + ("..." if len(combined_output) > 500 else "")
    return (
        "Falha ao inicializar o Chromium do Playwright no ambiente de deploy. "
        f"Detalhe: {short_output or 'erro sem detalhes'}"
    )


def _run_node_download(
    script_path: str,
    cert_alias: str,
    data_inicial: str,
    data_final: str,
    download_dir: str,
    certs_json_path: str,
    credentials_json_path: str,
    login_type: str = "certificado",
    headless: bool = False,
    tipo_nota: str = "tomados",
) -> Dict[str, Any]:
    settings = get_settings()
    env = os.environ.copy()

    node_opts = env.get("NODE_OPTIONS", "")
    if "--openssl-legacy-provider" not in node_opts:
        env["NODE_OPTIONS"] = (node_opts + " " if node_opts else "") + "--openssl-legacy-provider"

    env["CERTS_JSON"] = certs_json_path
    env["CREDENTIALS_JSON"] = credentials_json_path
    env["LOGIN_TYPE"] = login_type

    try:
        if login_type == "certificado":
            pfx_pass = get_password(cert_alias)
            if not pfx_pass:
                return {
                    "ok": False,
                    "error": (
                        f"Senha não configurada para o certificado '{cert_alias}'. "
                        "Defina CERT_PASSWORD_<ALIAS>, CERT_PASSWORDS_JSON ou cadastre a senha pela API."
                    ),
                    "stdout": "",
                    "stderr": "",
                    "returncode": None,
                    "errorCategory": "auth_terminal",
                    "retryableExternally": False,
                }
            env["PFX_PASS"] = pfx_pass
        else:
            portal_pass = get_credential_password(cert_alias)
            if not portal_pass:
                return {
                    "ok": False,
                    "error": (
                        f"Senha não configurada para a credencial '{cert_alias}'. "
                        "Defina CREDENTIAL_PASSWORD_<ALIAS>, CREDENTIAL_PASSWORDS_JSON ou cadastre a senha pela API."
                    ),
                    "stdout": "",
                    "stderr": "",
                    "returncode": None,
                    "errorCategory": "auth_terminal",
                    "retryableExternally": False,
                }
            env["LOGIN_PASS"] = portal_pass

        proc = subprocess.run(
            [
                settings.node_bin,
                script_path,
                "--alias",
                cert_alias,
                "--dataInicial",
                data_inicial,
                "--dataFinal",
                data_final,
                "--downloadDir",
                download_dir,
                "--certsJson",
                certs_json_path,
                "--credentialsJson",
                credentials_json_path,
                "--loginType",
                login_type,
                "--headless",
                "true" if headless else "false",
                "--tipoNota",
                tipo_nota,
            ],
            cwd=str(Path(script_path).parent),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=settings.playwright_timeout_ms / 1000,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": f"Node.js não encontrado no PATH. Binário configurado: {settings.node_bin}",
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "errorCategory": "runtime_terminal",
            "retryableExternally": False,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = _normalize_output_text(exc.stdout).strip()
        stderr = _normalize_output_text(exc.stderr).strip()
        context_summary = _summarize_playwright_context(stdout, stderr)
        context_suffix = f" | {context_summary}" if context_summary else ""
        return {
            "ok": False,
            "error": (
                f"Playwright excedeu o timeout de {settings.playwright_timeout_ms} ms "
                f"para {login_type}:{cert_alias} ({data_inicial}..{data_final}){context_suffix}"
            ),
            "stdout": stdout,
            "stderr": stderr,
            "returncode": None,
            "last_stage": _extract_last_stage(stdout, stderr),
            "log_tail": _build_log_tail(stdout, stderr),
            "errorCategory": "process_timeout",
            "retryableExternally": True,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"Erro executando subprocess do Playwright: {e}",
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "errorCategory": "transient_process",
            "retryableExternally": True,
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    payload: Dict[str, Any] = {
        "ok": False,
        "error": None,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": proc.returncode,
        "last_stage": _extract_last_stage(stdout, stderr),
        "log_tail": _build_log_tail(stdout, stderr),
        "errorCategory": None,
        "retryableExternally": None,
    }

    parsed: Optional[Dict[str, Any]] = None
    for text in (stdout, stderr):
        if not text:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            continue
        try:
            maybe = json.loads(lines[-1])
        except Exception:
            continue
        if isinstance(maybe, dict):
            parsed = maybe
            break

    if parsed is not None:
        payload.update(parsed)
        payload.setdefault("stdout", stdout)
        payload.setdefault("stderr", stderr)
        payload.setdefault("returncode", proc.returncode)
        payload.setdefault("ok", proc.returncode == 0)
        if not payload.get("ok") and not payload.get("error"):
            payload["error"] = stderr or stdout or f"Node retornou código {proc.returncode} sem detalhes"
        return payload

    if proc.returncode == 0:
        payload["ok"] = True
        return payload

    payload["error"] = stderr or stdout or f"Playwright retornou código {proc.returncode} sem saída parseável"
    payload["retryableExternally"] = _fallback_retryable_error(payload["error"])
    return payload


def executar_fluxo_nfse_playwright(
    cert_alias: str,
    data_inicial: str,
    data_final: str,
    diretorio_base: str,
    certs_json_path: str,
    credentials_json_path: str,
    login_type: str = "certificado",
    headless: bool = False,
    download_dir: str | None = None,
    tipo_nota: str = "tomados",
) -> Tuple[bool, int, bool, Optional[str]]:
    settings = get_settings()
    script_path = Path(settings.playwright_script_path).resolve()
    package_json_path = settings.package_json_path

    if not script_path.exists():
        return False, 0, False, f"Script Playwright não encontrado: {script_path}"
    if not package_json_path.exists():
        return False, 0, False, f"package.json não encontrado: {package_json_path}"
    runtime_error = _preflight_playwright_runtime(settings)
    if runtime_error:
        logger.error("Preflight do Playwright falhou: %s", runtime_error)
        return False, 0, False, runtime_error
    browser_error = _validate_playwright_browser_launch(settings)
    if browser_error:
        logger.error("Validação do Chromium do Playwright falhou: %s", browser_error)
        return False, 0, False, browser_error

    Path(diretorio_base).mkdir(parents=True, exist_ok=True)
    if download_dir is None:
        download_dir = str(Path(diretorio_base) / "tmp_downloads" / datetime.now().strftime("%Y%m%d_%H%M%S"))
    Path(download_dir).mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 60}")
    print("PLAYWRIGHT: LOGIN + DOWNLOAD")
    print(f"Alias: {cert_alias}")
    print(f"Login type: {login_type}")
    print(f"Período: {data_inicial} até {data_final}")
    print(f"Download dir: {download_dir}")
    print(f"Tipo nota: {tipo_nota}")
    print(f"{'=' * 60}")

    max_tentativas = MAX_EXTERNAL_ATTEMPTS
    result: Dict[str, Any] = {}
    login_desc = f"{login_type}:{cert_alias}"

    for tentativa in range(1, max_tentativas + 1):
        try:
            for path in Path(download_dir).iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

        print(f"▶ Tentativa {tentativa}/{max_tentativas} - {login_desc}")
        result = _run_node_download(
            script_path=str(script_path),
            cert_alias=cert_alias,
            data_inicial=data_inicial,
            data_final=data_final,
            download_dir=download_dir,
            certs_json_path=certs_json_path,
            credentials_json_path=credentials_json_path,
            login_type=login_type,
            headless=headless,
            tipo_nota=tipo_nota,
        )

        if result.get("ok"):
            total_xml = int(result.get("totalXml") or 0)
            total_pdf = int(result.get("totalPdf") or 0)
            print(f"✅ Download concluído ({login_desc}) - XML: {total_xml} | PDF: {total_pdf}")
            return True, total_xml, bool(result.get("needToSplit")), None

        error_msg = (
            result.get("error")
            or (result.get("stderr") or "").strip()
            or (((result.get("stdout") or "").strip()[:500] + "...") if (result.get("stdout") or "").strip() else "")
            or "erro desconhecido no Playwright"
        )
        if not result.get("ok"):
            last_stage = result.get("last_stage")
            log_tail = result.get("log_tail")
            if last_stage and f"ultima etapa: {last_stage}" not in error_msg:
                error_msg = f"{error_msg} | ultima etapa: {last_stage}"
            if log_tail and "ultimos logs:" not in error_msg:
                error_msg = f"{error_msg} | ultimos logs: {log_tail}"
        error_category = str(result.get("errorCategory") or "unknown")
        retryable_externally = _should_retry_externally(result, error_msg, tentativa)
        print(
            f"Playwright falhou para {login_desc}: {error_msg} "
            f"| categoria={error_category} | retry_externo={'sim' if retryable_externally else 'nao'}"
        )

        retryable_externally = _should_retry_externally(result, error_msg, tentativa)
        msg_lower = str(error_msg).lower()
        if "please run the following command to download new browsers" in msg_lower:
            error_msg = (
                "Playwright instalado sem browsers. "
                "No Render, rode 'npx playwright install --with-deps chromium' no build ou use o Dockerfile do projeto."
            )
        elif "executable doesn't exist" in msg_lower:
            error_msg = (
                "Browser do Playwright nao encontrado no servidor. "
                "Instale os browsers do Playwright durante o build."
            )
        print(f"❌ Playwright falhou para {login_desc}: {error_msg}")

        msg_lower = str(error_msg).lower()
        eh_falha_login = (
            "falha no login" in msg_lower
            or "/emissornacional/login" in msg_lower
            or "login/index" in msg_lower
            or ("certificado" in msg_lower and "login" in msg_lower)
        )
        if retryable_externally and tentativa < max_tentativas:
            wait_time = EXTERNAL_RETRY_WAIT_SECONDS[tentativa - 1]
            print(f"⏳ Aguardando {wait_time} segundos antes de tentar novamente...")
            print(
                f"Retry externo {tentativa}/{max_tentativas - 1}: "
                f"aguardando {wait_time}s antes de reiniciar o subprocesso..."
            )
            time.sleep(wait_time)
            continue
        return False, 0, False, str(error_msg)

    error_msg = (
        result.get("error")
        or (result.get("stderr") or "").strip()
        or (((result.get("stdout") or "").strip()[:500] + "...") if (result.get("stdout") or "").strip() else "")
        or "Falha após todas as tentativas do Playwright"
    )
    return False, 0, False, str(error_msg)
