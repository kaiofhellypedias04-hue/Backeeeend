from __future__ import annotations

import glob
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .downloader import criar_estrutura_pastas, distribuir_por_competencia
from .nfse_xml_converter import NFSeXMLConverterComAPI
from .notas_repo import (
    garantir_schema_nfse_notas,
    gerar_chave_nfse,
    salvar_nota_nfse,
)
from .playwright_downloader import executar_fluxo_nfse_playwright
from .run_state_repo import garantir_schema_run_state, upsert_state
from .spreadsheet import atualizar_planilha_incremental

DEFAULT_CHUNK_DAYS_FALLBACK = 5
XML_MICROBATCH_SIZE = 100


@dataclass
class RunConfig:
    modo: str  # 'manual' | 'automatico'
    base_dir: str
    certs_json_path: str
    cert_aliases: list[str]
    start: date | None = None
    end: date | None = None
    headless: bool = False
    use_chunk_days: bool = False
    chunk_days: int = 15  # Chunk manual por data no Python; split >800 continua no Node
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


def _normalize_chunk_days(value: Any) -> int | None:
    try:
        chunk_days = int(value)
    except (TypeError, ValueError):
        return None
    return chunk_days if chunk_days > 0 else None


def _normalize_use_chunk_days(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "sim", "on"}


def _resolve_chunk_settings(use_chunk_days: Any, chunk_days: Any) -> tuple[bool, int | None, str | None]:
    chunk_enabled = _normalize_use_chunk_days(use_chunk_days)
    if not chunk_enabled:
        return False, None, None

    normalized_chunk_days = _normalize_chunk_days(chunk_days)
    if normalized_chunk_days is not None:
        return True, normalized_chunk_days, None

    return (
        True,
        DEFAULT_CHUNK_DAYS_FALLBACK,
        (
            "chunk_days invalido para chunk manual; "
            f"fallback aplicado: {DEFAULT_CHUNK_DAYS_FALLBACK} dia(s)"
        ),
    )


def _resolve_processing_chunks(start: date, end: date, use_chunk_days: Any, chunk_days: Any) -> list[tuple[date, date]]:
    chunk_enabled, normalized_chunk_days, _ = _resolve_chunk_settings(use_chunk_days, chunk_days)
    if not chunk_enabled or normalized_chunk_days is None:
        return [(start, end)]
    return list(_chunk_ranges(start, end, normalized_chunk_days))


def _iter_file_batches(file_paths: list[str], batch_size: int):
    if batch_size <= 0:
        batch_size = len(file_paths) or 1
    for idx in range(0, len(file_paths), batch_size):
        yield file_paths[idx: idx + batch_size]


def _resolver_intervalo_automatico(cfg: RunConfig, cert_alias: str) -> tuple[date, date]:
    """Resolve o intervalo automatico para processamento."""
    hoje = date.today()

    if cfg.start is not None and cfg.end is not None:
        start = cfg.start
        end = cfg.end
    else:
        end = hoje
        start = hoje - timedelta(days=29)

    if end > hoje:
        end = hoje
    if start > end:
        start = end

    return start, end


def run_processing(cfg: RunConfig, logger=None) -> list[dict[str, Any]]:
    """Orquestra execucao manual (GUI) e automatica (CLI) sem depender de Tkinter."""
    if logger is None:
        from worker.logging import StructuredLogger

        logger = StructuredLogger("WARNING")

    os.makedirs(cfg.base_dir, exist_ok=True)
    garantir_schema_run_state()
    garantir_schema_nfse_notas()

    converter = NFSeXMLConverterComAPI(tipo_nota=cfg.tipo_nota, consultar_api=cfg.consultar_api)
    resultados_execucao: list[dict[str, Any]] = []

    for i_cert, cert_alias in enumerate(cfg.cert_aliases, start=1):
        print(f"\n{'=' * 60}")
        print(f"{'CREDENCIAL' if cfg.login_type == 'cpf_cnpj' else 'CERTIFICADO'}: {cert_alias}")
        print("=" * 60)

        base_dir_cert = os.path.join(cfg.base_dir, cert_alias)
        os.makedirs(base_dir_cert, exist_ok=True)

        if cfg.modo == "automatico":
            start, end = _resolver_intervalo_automatico(cfg, cert_alias)
        else:
            if not cfg.start or not cfg.end:
                raise ValueError("Modo manual requer start e end")
            start, end = cfg.start, cfg.end

        chunk_enabled, chunk_days_valid, chunk_warning = _resolve_chunk_settings(
            getattr(cfg, "use_chunk_days", False),
            getattr(cfg, "chunk_days", None),
        )
        chunks = _resolve_processing_chunks(
            start,
            end,
            getattr(cfg, "use_chunk_days", False),
            getattr(cfg, "chunk_days", None),
        )

        print(f"\nPeriodo total solicitado: {start.isoformat()} -> {end.isoformat()}")
        print(
            "Configuracao de chunk manual: "
            f"use_chunk_days={chunk_enabled} | "
            f"chunk_days_recebido={getattr(cfg, 'chunk_days', None)} | "
            f"chunk_days_efetivo={chunk_days_valid if chunk_enabled else 'desativado'}"
        )
        if chunk_warning:
            print(f"[chunk] Aviso: {chunk_warning}")
            logger.warning(chunk_warning)
        if not chunk_enabled:
            print("Modo de processamento: normal (sem chunk manual).")
        print(f"Quantidade de chunks gerados: {len(chunks)}")
        for idx_chunk, (chunk_start, chunk_end) in enumerate(chunks, start=1):
            print(f"  Chunk {idx_chunk}/{len(chunks)}: {chunk_start.isoformat()} -> {chunk_end.isoformat()}")

        upsert_state(cert_alias, status="running", last_error=None)
        last_ok_date: date | None = None

        def _process_tmp_dir(tmp_dir: str, base_dir_cert: str, periodo_start: date, periodo_end: date) -> dict[str, Any]:
            moved = distribuir_por_competencia(tmp_dir, base_dir_cert)
            xml_paths = list(moved.get("xml") or [])
            pdf_paths = list(moved.get("pdf") or [])

            resultado: dict[str, Any] = {
                "cert_alias": cert_alias,
                "periodo_start": periodo_start,
                "periodo_end": periodo_end,
                "xml_paths": xml_paths,
                "pdf_paths": pdf_paths,
                "planilha_paths": [],
                "xml_movidos": len(xml_paths),
                "pdf_movidos": len(pdf_paths),
                "dados_extraidos": 0,
                "notas_salvas": 0,
                "erros_salvamento": [],
                "status": "sem_xml",
            }

            if not xml_paths:
                logger.info("Nenhum XML novo para processar neste chunk.")
                return resultado

            notas_salvas = 0
            erros_salvamento: list[str] = []
            dados_extraidos_total = 0

            estrutura = criar_estrutura_pastas(
                base_dir_cert,
                data_referencia=datetime(periodo_end.year, periodo_end.month, 1),
            )
            planilhas_dir = estrutura["planilhas_dir"]
            planilhas_existentes = glob.glob(os.path.join(planilhas_dir, "auditoria_nfse*.xlsx"))

            if planilhas_existentes:
                caminho_planilha = planilhas_existentes[0]
                nome_periodo = os.path.basename(caminho_planilha).replace("auditoria_nfse_", "").replace(".xlsx", "")
                logger.info("Planilha existente encontrada", {"planilha": os.path.basename(caminho_planilha)})
            else:
                nome_periodo = f"{periodo_start.isoformat()}_a_{periodo_end.isoformat()}"
                planilha_nome = f"auditoria_nfse_{cert_alias}_{nome_periodo}.xlsx"
                caminho_planilha = os.path.join(planilhas_dir, planilha_nome)

            batch_size = XML_MICROBATCH_SIZE if chunk_enabled else max(len(xml_paths), 1)
            total_batches = max(1, (len(xml_paths) + batch_size - 1) // batch_size)
            logger.info(
                "Processando XMLs",
                {
                    "count": len(xml_paths),
                    "batch_size": batch_size,
                    "total_batches": total_batches,
                    "chunk_mode": chunk_enabled,
                },
            )

            for idx_batch, xml_batch in enumerate(_iter_file_batches(xml_paths, batch_size), start=1):
                logger.info(
                    "Processando lote de XMLs",
                    {
                        "batch_index": idx_batch,
                        "batch_total": total_batches,
                        "count": len(xml_batch),
                        "chunk_period": f"{periodo_start.isoformat()}..{periodo_end.isoformat()}",
                    },
                )
                dados = converter.process_multiple_files(xml_batch)
                if not dados:
                    logger.warning(
                        "Nenhum dado extraido do lote de XMLs",
                        {"batch_index": idx_batch, "batch_total": total_batches},
                    )
                    continue

                dados = converter.consultar_cnpjs_em_lote(dados)
                dados_extraidos_total += len(dados or [])

                for d in dados:
                    try:
                        arquivo_origem = d.get("_arquivo_origem") or d.get("_Arquivo_Origem")
                        salvar_nota_nfse(
                            cert_alias,
                            getattr(cfg, "processo_id", None),
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

                existentes, adicionados = atualizar_planilha_incremental(
                    converter,
                    caminho_planilha,
                    dados,
                    cert_alias=cert_alias,
                )
                logger.info(
                    "Planilha atualizada",
                    {
                        "periodo": nome_periodo,
                        "batch_index": idx_batch,
                        "batch_total": total_batches,
                        "existentes": existentes,
                        "adicionados": adicionados,
                        "planilha": os.path.basename(caminho_planilha),
                    },
                )

            resultado["dados_extraidos"] = dados_extraidos_total
            resultado["notas_salvas"] = notas_salvas
            resultado["erros_salvamento"] = erros_salvamento

            if dados_extraidos_total == 0:
                logger.warning("Nenhum dado extraido dos XMLs movidos.")
                resultado["status"] = "sem_dados"
                return resultado

            if xml_paths and notas_salvas == 0:
                resultado["status"] = "falha_sem_notas"
                return resultado

            logger.info(
                "Consolidacao do chunk concluida",
                {
                    "periodo": f"{periodo_start.isoformat()}..{periodo_end.isoformat()}",
                    "xml_movidos": len(xml_paths),
                    "pdf_movidos": len(pdf_paths),
                    "dados_extraidos": dados_extraidos_total,
                    "notas_salvas": notas_salvas,
                    "erros_salvamento": len(erros_salvamento),
                },
            )

            resultado["planilha_paths"] = [caminho_planilha] if os.path.exists(caminho_planilha) else []
            resultado["status"] = "ok"

            nonlocal last_ok_date
            last_ok_date = periodo_end
            return resultado

        resultado_cert: dict[str, Any] = {
            "cert_alias": cert_alias,
            "start": start,
            "end": end,
            "tmp_dir": None,
            "download_ok": False,
            "total_xmls_baixados": 0,
            "processamento": None,
            "chunks": [],
            "status": "pending",
        }

        try:
            processamentos_chunks: list[dict[str, Any]] = []
            total_xmls_baixados = 0
            houve_split_interno = False
            total_xml_movidos = 0
            total_pdf_movidos = 0
            total_dados_extraidos = 0
            total_notas_salvas = 0
            xml_paths_consolidados: list[str] = []
            pdf_paths_consolidados: list[str] = []
            planilha_paths: list[str] = []
            erros_salvamento: list[str] = []

            for idx_chunk, (chunk_start, chunk_end) in enumerate(chunks, start=1):
                print(
                    f"\n[chunk {idx_chunk}/{len(chunks)}] Inicio: "
                    f"{chunk_start.isoformat()} -> {chunk_end.isoformat()}"
                )
                chunk_started_at = time.perf_counter()
                run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
                tmp_dir = os.path.join(base_dir_cert, "tmp_downloads", f"{run_id}_chunk_{idx_chunk:03d}")
                os.makedirs(tmp_dir, exist_ok=True)
                resultado_cert["tmp_dir"] = tmp_dir

                ok, total_xmls, need_to_split, error_msg = executar_fluxo_nfse_playwright(
                    cert_alias=cert_alias,
                    data_inicial=_date_to_br(chunk_start),
                    data_final=_date_to_br(chunk_end),
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
                    raise RuntimeError(
                        f"Falha no download Playwright para {cert_alias} "
                        f"(chunk {idx_chunk}/{len(chunks)} - {chunk_start}..{chunk_end}){detail}"
                    )

                processamento = _process_tmp_dir(tmp_dir, base_dir_cert, chunk_start, chunk_end)

                if (total_xmls or 0) > 0 and processamento["xml_movidos"] == 0:
                    raise RuntimeError(
                        f"Chunk {idx_chunk}/{len(chunks)} baixou {total_xmls} XML(s), "
                        "mas nenhum XML novo foi distribuido/processado."
                    )

                if processamento["xml_movidos"] > 0 and processamento["dados_extraidos"] == 0:
                    raise RuntimeError(
                        f"Chunk {idx_chunk}/{len(chunks)} moveu {processamento['xml_movidos']} XML(s), "
                        "mas nenhum dado foi extraido."
                    )

                if processamento["xml_movidos"] > 0 and processamento["notas_salvas"] == 0:
                    raise RuntimeError(
                        f"Chunk {idx_chunk}/{len(chunks)} moveu {processamento['xml_movidos']} XML(s), "
                        "mas nenhuma nota foi persistida."
                    )

                chunk_elapsed_s = time.perf_counter() - chunk_started_at
                print(
                    f"[chunk {idx_chunk}/{len(chunks)}] Fim: "
                    f"{chunk_start.isoformat()} -> {chunk_end.isoformat()} "
                    f"| tempo={chunk_elapsed_s:.1f}s | xml_baixados={int(total_xmls or 0)}"
                )

                chunk_result = {
                    "index": idx_chunk,
                    "start": chunk_start,
                    "end": chunk_end,
                    "tmp_dir": tmp_dir,
                    "download_ok": True,
                    "total_xmls_baixados": int(total_xmls or 0),
                    "need_to_split": bool(need_to_split),
                    "processamento": processamento,
                    "elapsed_seconds": round(chunk_elapsed_s, 3),
                    "status": "ok",
                }
                processamentos_chunks.append(chunk_result)

                total_xmls_baixados += int(total_xmls or 0)
                houve_split_interno = houve_split_interno or bool(need_to_split)
                total_xml_movidos += int(processamento["xml_movidos"] or 0)
                total_pdf_movidos += int(processamento["pdf_movidos"] or 0)
                total_dados_extraidos += int(processamento["dados_extraidos"] or 0)
                total_notas_salvas += int(processamento["notas_salvas"] or 0)
                xml_paths_consolidados.extend(processamento.get("xml_paths") or [])
                pdf_paths_consolidados.extend(processamento.get("pdf_paths") or [])
                planilha_paths.extend(processamento.get("planilha_paths") or [])
                erros_salvamento.extend(processamento.get("erros_salvamento") or [])

            resultado_cert["download_ok"] = True
            resultado_cert["total_xmls_baixados"] = total_xmls_baixados
            resultado_cert["need_to_split"] = houve_split_interno
            resultado_cert["chunks"] = processamentos_chunks
            resultado_cert["processamento"] = {
                "cert_alias": cert_alias,
                "periodo_start": start,
                "periodo_end": end,
                "use_chunk_days": chunk_enabled,
                "chunk_days": chunk_days_valid,
                "total_chunks": len(chunks),
                "xml_paths": list(dict.fromkeys(xml_paths_consolidados)),
                "pdf_paths": list(dict.fromkeys(pdf_paths_consolidados)),
                "xml_movidos": total_xml_movidos,
                "pdf_movidos": total_pdf_movidos,
                "dados_extraidos": total_dados_extraidos,
                "notas_salvas": total_notas_salvas,
                "planilha_paths": list(dict.fromkeys(planilha_paths)),
                "erros_salvamento": erros_salvamento,
                "status": "ok",
            }
            logger.info(
                "Consolidacao final do certificado",
                {
                    "cert_alias": cert_alias,
                    "use_chunk_days": chunk_enabled,
                    "chunk_days": chunk_days_valid,
                    "total_chunks": len(chunks),
                    "xml_baixados": total_xmls_baixados,
                    "xml_movidos": total_xml_movidos,
                    "pdf_movidos": total_pdf_movidos,
                    "dados_extraidos": total_dados_extraidos,
                    "notas_salvas": total_notas_salvas,
                },
            )

            upsert_state(cert_alias, last_processed_date=last_ok_date or end, status="ok", last_error=None)
            resultado_cert["status"] = "ok"

        except Exception as e:
            print(f"Erro no processamento para {cert_alias}: {e}")
            upsert_state(cert_alias, status="error", last_error=str(e))
            resultado_cert["status"] = "error"
            resultado_cert["error"] = str(e)
            resultados_execucao.append(resultado_cert)
            if getattr(cfg, "processo_id", None):
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

                    print(f"Espera pos-certificado {n}: {int(sleep_s)}s")
                    time.sleep(sleep_s)
            except Exception:
                pass

    return resultados_execucao
