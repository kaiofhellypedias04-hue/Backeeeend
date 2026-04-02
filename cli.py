"""CLI (headless) para execução agendada.

Compatível com a versão anterior (--modo/--certificados), mas agora suporta:

- Processar apenas 1 certificado:
    python cli.py --modo automatico --base-dir /data/nfse --cert ALIAS

- Modo automático diário (sem interação):
    python cli.py --auto --base-dir /data/nfse --certificados todos

Observações:
- Em --auto, o intervalo é resolvido automaticamente por certificado via run_state.
- Use --start apenas na primeira execução (se não houver estado) para definir o início.
"""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

from modules.config_loader import carregar_certificados
from modules.runner import RunConfig, run_processing
from modules.settings import get_settings

load_dotenv()


def _parse_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "sim")


def _parse_date(v: str) -> date:
    return datetime.strptime(v, "%Y-%m-%d").date()


def _yesterday() -> date:
    return date.today() - timedelta(days=1)


def main():
    p = argparse.ArgumentParser(description="Auditoria NFS-e - CLI (headless)")

    # Modo novo (preferido)
    p.add_argument("--auto", action="store_true", help="Modo automático diário (sem interação)")

    # Compatibilidade antiga
    p.add_argument(
        "--modo",
        choices=["automatico"],
        help="(legado) Use 'automatico' para execução headless. Se --auto for usado, --modo é ignorado.",
    )

    p.add_argument("--base-dir", required=True, help="Pasta principal onde será criada a estrutura por certificado")

    # Seleção de certificados (novo + legado)
    p.add_argument("--cert", help="Processar apenas 1 certificado (alias)")
    p.add_argument(
        "--certificados",
        default="todos",
        help="(legado) 'todos' ou lista separada por vírgula (ex: alias1,alias2). Ignorado se --cert for usado.",
    )

    p.add_argument("--start", help="YYYY-MM-DD (opcional; útil na primeira execução se não houver estado)")
    p.add_argument("--end", help="YYYY-MM-DD (default=hoje; em --auto recomendável usar default ou 'ontem')")
    p.add_argument(
        "--ate-ontem",
        action="store_true",
        help="Se definido, força end=ontem (recomendado para rotina diária).",
    )

    p.add_argument("--headless", default="true", help="true/false para Playwright")
    p.add_argument("--chunk-days", type=int, default=30, help="Tamanho do chunk (max 30 recomendado)")
    args = p.parse_args()

    # Determina modo
    modo = "automatico" if (args.auto or args.modo == "automatico") else None
    if not modo:
        raise SystemExit("Informe --auto ou --modo automatico")

    settings = get_settings()
    certs_path = str(settings.certs_json_path)
    certs = carregar_certificados(certs_path)
    if not certs:
        raise SystemExit("certs.json não encontrado ou vazio")

    all_aliases = [c.get("alias") for c in certs if c.get("alias")]

    # Seleção de certificados
    if args.cert:
        alias = args.cert.strip()
        if alias not in all_aliases:
            raise SystemExit(f"Alias não encontrado no certs.json: {alias}")
        aliases = [alias]
    else:
        if str(args.certificados).strip().lower() == "todos":
            aliases = all_aliases
        else:
            wanted = [x.strip() for x in str(args.certificados).split(",") if x.strip()]
            aliases = [a for a in all_aliases if a in wanted]
            missing = [w for w in wanted if w not in all_aliases]
            if missing:
                print(f"⚠️ Aviso: aliases não encontrados no certs.json: {', '.join(missing)}")

    if not aliases:
        raise SystemExit("Nenhum certificado selecionado")

    # End date
    end = _parse_date(args.end) if args.end else None
    if args.ate_ontem:
        end = _yesterday()

    rcfg = RunConfig(
        modo="automatico",
        base_dir=args.base_dir,
        certs_json_path=certs_path,
        credentials_json_path=str(settings.credentials_json_path),
        cert_aliases=aliases,
        start=_parse_date(args.start) if args.start else None,
        end=end,
        headless=_parse_bool(args.headless),
        chunk_days=int(args.chunk_days or 30),
        consultar_api=True,
    )

    run_processing(rcfg)


if __name__ == "__main__":
    main()
