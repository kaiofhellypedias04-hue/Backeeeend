from datetime import date
from pathlib import Path
from unittest.mock import patch

from modules.runner import RunConfig, run_processing


class _FakeLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


class _FakeConverter:
    def process_multiple_files(self, xml_batch):
        return []

    def consultar_cnpjs_em_lote(self, dados):
        return dados


def _build_cfg(tmp_path: Path) -> RunConfig:
    return RunConfig(
        modo="manual",
        base_dir=str(tmp_path / "saida"),
        certs_json_path=str(tmp_path / "certs.json"),
        credentials_json_path=str(tmp_path / "credentials.json"),
        cert_aliases=["cert-a"],
        start=date(2026, 4, 1),
        end=date(2026, 4, 5),
        headless=True,
    )


def test_run_processing_aceita_xml_baixado_duplicado_sem_falhar(tmp_path):
    cfg = _build_cfg(tmp_path)

    with patch("modules.runner.garantir_schema_run_state"), patch(
        "modules.runner.garantir_schema_nfse_notas"
    ), patch("modules.runner.upsert_state"), patch(
        "modules.runner.NFSeXMLConverterComAPI",
        return_value=_FakeConverter(),
    ), patch(
        "modules.runner.executar_fluxo_nfse_playwright",
        return_value=(True, 74, False, None),
    ), patch(
        "modules.runner.distribuir_por_competencia",
        return_value={
            "xml": [],
            "pdf": [],
            "xml_encontrados_tmp": 74,
            "xml_duplicados": 74,
            "xml_falhas_distribuicao": 0,
        },
    ):
        resultados = run_processing(cfg, logger=_FakeLogger())

    assert len(resultados) == 1
    resultado = resultados[0]
    assert resultado["status"] == "ok"
    assert resultado["download_ok"] is True
    assert resultado["total_xmls_baixados"] == 74
    assert resultado["processamento"]["xml_movidos"] == 0
    assert resultado["processamento"]["xml_duplicados"] == 74


def test_run_processing_mantem_erro_quando_xml_falha_na_distribuicao(tmp_path):
    cfg = _build_cfg(tmp_path)

    with patch("modules.runner.garantir_schema_run_state"), patch(
        "modules.runner.garantir_schema_nfse_notas"
    ), patch("modules.runner.upsert_state"), patch(
        "modules.runner.NFSeXMLConverterComAPI",
        return_value=_FakeConverter(),
    ), patch(
        "modules.runner.executar_fluxo_nfse_playwright",
        return_value=(True, 3, False, None),
    ), patch(
        "modules.runner.distribuir_por_competencia",
        return_value={
            "xml": [],
            "pdf": [],
            "xml_encontrados_tmp": 3,
            "xml_duplicados": 0,
            "xml_falhas_distribuicao": 3,
        },
    ):
        resultados = run_processing(cfg, logger=_FakeLogger())

    assert len(resultados) == 1
    resultado = resultados[0]
    assert resultado["status"] == "error"
    assert "falharam na distribuicao" in resultado["error"]
