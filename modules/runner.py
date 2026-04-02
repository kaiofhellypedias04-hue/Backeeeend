from __future__ import annotations

import os
import time
import random
import uuid
import glob
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .nfse_xml_converter import NFSeXMLConverterComAPI
from .playwright_downloader import executar_fluxo_nfse_playwright
from .downloader import distribuir_por_competencia, criar_estrutura_pastas
from .spreadsheet import atualizar_planilha_incremental
from .notas_repo import (
    salvar_nota_nfse,
    gerar_chave_nfse,
    garantir_schema_nfse_notas,
)
from .run_state_repo import upsert_state, garantir_schema_run_state


@dataclass
class RunConfig:
    modo: str  # 'manual' | 'automatico'
    base_dir: str
    certs_json_path: str
    cert_aliases: list[str]
    start: date | None = None
    end: date | None = None
    headless: bool = False
    chunk_days: int = 15  # (legacy) Chunking no Python está desativado; split >800 ocorre no Node na mesma sessão
    consultar_api: bool = True
    login_type: str = "certificado"  # 'certificado' ou 'cpf_cnpj'
    credentials_json_path: str = ""  # Caminho para credentials.json
    tipo_nota: str = "tomados"  # 'tomados' (Recebidas) ou 'prestados' (Emitidas)


def _date_to_br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _chunk_ranges(start: date, end: date, chunk_days: int):
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=chunk_days - 1))
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def _resolver_intervalo_automatico(cfg: RunConfig, cert_alias: str) -> tuple[date, date]:
    """Resolve o intervalo automático para processamento.

    Requisito 4: Sempre processar os ÚLTIMOS 30 DIAS.
    As datas já devem estar configuradas em cfg.start e cfg.end pelo main.py.
    Esta função apenas valida e retorna os valores.
    """
    hoje = date.today()

    # Se cfg.start e cfg.end já estão definidos (últimos 30 dias), usar esses valores
    if cfg.start is not None and cfg.end is not None:
        start = cfg.start
        end = cfg.end
    else:
        # Fallback: calcular últimos 30 dias se não definido
        end = hoje
        start = hoje - timedelta(days=29)

    # Validação: não processar datas futuras
    if end > hoje:
        end = hoje
    if start > end:
        start = end

    return start, end


def run_processing(cfg: RunConfig, logger=None) -> list[dict[str, Any]]:
    """Orquestra execução manual (GUI) e automática (CLI) sem depender de Tkinter.

    Retorna uma lista de resultados por certificado. O retorno é compatível com
    chamadas legadas que ignoram o valor de retorno.

    Args:
        logger: Optional StructuredLogger for structured logging.
    """
    if logger is None:
        from worker.logging import StructuredLogger
        logger = StructuredLogger('WARNING')  # Minimal fallback

    os.makedirs(cfg.base_dir, exist_ok=True)
    garantir_schema_run_state()
    garantir_schema_nfse_notas()

    converter = NFSeXMLConverterComAPI(tipo_nota=cfg.tipo_nota, consultar_api=cfg.consultar_api)
    resultados_execucao: list[dict[str, Any]] = []

    for i_cert, cert_alias in enumerate(cfg.cert_aliases, start=1):
        print(f"\n{'='*60}")
        print(f"{'CREDENCIAL' if cfg.login_type == 'cpf_cnpj' else 'CERTIFICADO'}: {cert_alias}")
        print("="*60)

        base_dir_cert = os.path.join(cfg.base_dir, cert_alias)
        os.makedirs(base_dir_cert, exist_ok=True)

        if cfg.modo == 'automatico':
            start, end = _resolver_intervalo_automatico(cfg, cert_alias)
        else:
            if not cfg.start or not cfg.end:
                raise ValueError("Modo manual requer start e end")
            start, end = cfg.start, cfg.end

        upsert_state(cert_alias, status='running', last_error=None)
        last_ok_date: date | None = None

        def _process_tmp_dir(tmp_dir: str, base_dir_cert: str, periodo_start: date, periodo_end: date) -> dict[str, Any]:
            """Move XMLs para estrutura final, processa, persiste e atualiza planilha.

            Retorna métricas e caminhos desta execução para permitir validação e
            registro consistente de arquivos no processo.
            """
            moved = distribuir_por_competencia(tmp_dir, base_dir_cert)
            xml_paths = list(moved.get('xml') or [])
            pdf_paths = list(moved.get('pdf') or [])

            resultado: dict[str, Any] = {
                'cert_alias': cert_alias,
                'periodo_start': periodo_start,
                'periodo_end': periodo_end,
                'xml_paths': xml_paths,
                'pdf_paths': pdf_paths,
                'planilha_paths': [],
                'xml_movidos': len(xml_paths),
                'pdf_movidos': len(pdf_paths),
                'dados_extraidos': 0,
                'notas_salvas': 0,
                'erros_salvamento': [],
                'status': 'sem_xml',
            }

            if not xml_paths:
                logger.info("Nenhum XML novo para processar neste chunk.")
                return resultado

            logger.info("Processando XMLs", {'count': len(xml_paths)})
            dados = converter.process_multiple_files(xml_paths)
            resultado['dados_extraidos'] = len(dados or [])

            if not dados:
                logger.warning("Nenhum dado extraído dos XMLs movidos.")
                resultado['status'] = 'sem_dados'
                return resultado

            dados = converter.consultar_cnpjs_em_lote(dados)
            resultado['dados_extraidos'] = len(dados or [])

            notas_salvas = 0
            erros_salvamento: list[str] = []

            # Persistir no banco (dedupe por cert_alias+chave_nfse)
            for d in dados:
                try:
                    arquivo_origem = d.get('_arquivo_origem') or d.get('_Arquivo_Origem')
                    salvar_nota_nfse(
                        cert_alias,
                        getattr(cfg, 'processo_id', None),
                        d,
                        arquivo_origem=arquivo_origem,
                    )
                    notas_salvas += 1
                except Exception as save_err:
                    erro_txt = (
                        f"nota_chave={gerar_chave_nfse(d)} | "
                        f"arquivo={d.get('_arquivo_origem') or d.get('_Arquivo_Origem')} | "
                        f"erro={save_err}"
                    )
                    erros_salvamento.append(erro_txt)
                    logger.warning(f"Erro salvando nota | {erro_txt}")

            resultado['notas_salvas'] = notas_salvas
            resultado['erros_salvamento'] = erros_salvamento

            # Falha explícita: havia XML processável, mas nenhuma nota foi persistida.
            if xml_paths and notas_salvas == 0:
                resultado['status'] = 'falha_sem_notas'
                return resultado

            # Planilha do período (incremental, 1 arquivo por execução/período)
            estrutura = criar_estrutura_pastas(
                base_dir_cert,
                data_referencia=datetime(periodo_end.year, periodo_end.month, 1),
            )
            planilhas_dir = estrutura['planilhas_dir']

            # Verifica se já existe alguma planilha na pasta para usar como base
            planilhas_existentes = glob.glob(os.path.join(planilhas_dir, "auditoria_nfse*.xlsx"))

            if planilhas_existentes:
                caminho_planilha = planilhas_existentes[0]
                nome_periodo = os.path.basename(caminho_planilha).replace("auditoria_nfse_", "").replace(".xlsx", "")
                logger.info("Planilha existente encontrada", {'planilha': os.path.basename(caminho_planilha)})
            else:
                nome_periodo = f"{periodo_start.isoformat()}_a_{periodo_end.isoformat()}"
                planilha_nome = f"auditoria_nfse_{cert_alias}_{nome_periodo}.xlsx"
                caminho_planilha = os.path.join(planilhas_dir, planilha_nome)

            existentes, adicionados = atualizar_planilha_incremental(converter, caminho_planilha, dados)
            logger.info(
                "Planilha atualizada",
                {
                    'periodo': nome_periodo,
                    'existentes': existentes,
                    'adicionados': adicionados,
                    'planilha': os.path.basename(caminho_planilha),
                },
            )

            resultado['planilha_paths'] = [caminho_planilha] if os.path.exists(caminho_planilha) else []
            resultado['status'] = 'ok'

            nonlocal last_ok_date
            last_ok_date = periodo_end
            return resultado

        resultado_cert: dict[str, Any] = {
            'cert_alias': cert_alias,
            'start': start,
            'end': end,
            'tmp_dir': None,
            'download_ok': False,
            'total_xmls_baixados': 0,
            'processamento': None,
            'status': 'pending',
        }

        try:
            print(f"\n📅 Período solicitado: {start.isoformat()} → {end.isoformat()}")
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
            tmp_dir = os.path.join(base_dir_cert, "tmp_downloads", run_id)
            os.makedirs(tmp_dir, exist_ok=True)
            resultado_cert['tmp_dir'] = tmp_dir

            ok, total_xmls, need_to_split, error_msg = executar_fluxo_nfse_playwright(
                cert_alias=cert_alias,
                data_inicial=_date_to_br(start),
                data_final=_date_to_br(end),
                diretorio_base=base_dir_cert,
                certs_json_path=cfg.certs_json_path,
                credentials_json_path=cfg.credentials_json_path,
                login_type=cfg.login_type,
                headless=cfg.headless,
                download_dir=tmp_dir,
                tipo_nota=cfg.tipo_nota,
            )
            if not ok:
                detail = f": {error_msg}" if error_msg else ""
                raise RuntimeError(f"Falha no download Playwright para {cert_alias} ({start}..{end}){detail}")

            resultado_cert['download_ok'] = True
            resultado_cert['total_xmls_baixados'] = total_xmls or 0
            resultado_cert['need_to_split'] = need_to_split

            processamento = _process_tmp_dir(tmp_dir, base_dir_cert, start, end)
            resultado_cert['processamento'] = processamento

            # Validações de integridade: não permitir sucesso falso.
            if (resultado_cert['total_xmls_baixados'] or 0) > 0 and processamento['xml_movidos'] == 0:
                raise RuntimeError(
                    f"Foram baixados {resultado_cert['total_xmls_baixados']} XML(s), mas nenhum XML novo foi distribuído/processado."
                )

            if processamento['xml_movidos'] > 0 and processamento['dados_extraidos'] == 0:
                raise RuntimeError(
                    f"{processamento['xml_movidos']} XML(s) foram movidos, mas nenhum dado foi extraído."
                )

            if processamento['xml_movidos'] > 0 and processamento['notas_salvas'] == 0:
                raise RuntimeError(
                    f"{processamento['xml_movidos']} XML(s) foram movidos, mas nenhuma nota foi persistida."
                )

            upsert_state(cert_alias, last_processed_date=last_ok_date or end, status='ok', last_error=None)
            resultado_cert['status'] = 'ok'

        except Exception as e:
            print(f"❌ Erro no processamento para {cert_alias}: {e}")
            upsert_state(cert_alias, status='error', last_error=str(e))
            resultado_cert['status'] = 'error'
            resultado_cert['error'] = str(e)
            resultados_execucao.append(resultado_cert)
            if getattr(cfg, 'processo_id', None):
                raise

        else:
            resultados_execucao.append(resultado_cert)

        finally:
            try:
                n = i_cert
                sleep_s = 0.0
                if n >= 1:
                    if n <= 5:
                        sleep_s = random.uniform(180, 300)
                    elif 6 <= n <= 9:
                        base = random.uniform(180, 300)
                        extra = 60 + (n - 6) * 30
                        sleep_s = base + extra
                    else:
                        sleep_s = random.uniform(480, 540)

                    print(f"⏸ Espera pós-certificado {n}: {int(sleep_s)}s")
                    time.sleep(sleep_s)
            except Exception:
                pass

    return resultados_execucao
