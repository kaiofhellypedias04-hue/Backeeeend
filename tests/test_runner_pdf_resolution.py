from datetime import date
from pathlib import Path
from unittest.mock import patch

from modules.runner import RunConfig, _resolve_pdf_path_for_xml, run_processing


class _FakeLogger:
    def __init__(self):
        self.infos = []
        self.warnings = []

    def info(self, *args, **kwargs):
        self.infos.append((args, kwargs))

    def warning(self, *args, **kwargs):
        self.warnings.append((args, kwargs))


class _FakeConverter:
    def __init__(self, dados):
        self._dados = dados

    def process_multiple_files(self, xml_batch):
        return list(self._dados)

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


def test_resolve_pdf_path_for_xml_com_mesmo_nome(tmp_path):
    xml_path = tmp_path / "nota.xml"
    pdf_path = tmp_path / "nota.pdf"
    xml_path.write_text("<xml />", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4")

    resolved = _resolve_pdf_path_for_xml(str(xml_path))

    assert resolved == str(pdf_path)


def test_resolve_pdf_path_for_xml_encontra_pdf_com_sufixo_diferente(tmp_path):
    xml_path = tmp_path / "Prestador NFS-e 123_1.xml"
    pdf_path = tmp_path / "Prestador NFS-e 123_dup2.pdf"
    xml_path.write_text("<xml />", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4")

    resolved = _resolve_pdf_path_for_xml(str(xml_path))

    assert resolved == str(pdf_path)


def test_resolve_pdf_path_for_xml_retorna_none_sem_quebrar_quando_nao_acha_pdf(tmp_path):
    xml_path = tmp_path / "nota_sem_pdf.xml"
    xml_path.write_text("<xml />", encoding="utf-8")

    resolved = _resolve_pdf_path_for_xml(str(xml_path))

    assert resolved is None


def test_run_processing_preenche_status_documental_pdf_quando_pdf_existe(tmp_path):
    cfg = _build_cfg(tmp_path)
    xml_dir = tmp_path / "saida" / "cert-a" / "2026" / "04" / "xml"
    pdf_dir = tmp_path / "saida" / "cert-a" / "2026" / "04" / "pdf"
    xml_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    xml_path = xml_dir / "Prestador NFS-e 123_1.xml"
    pdf_path = pdf_dir / "Prestador NFS-e 123_dup2.pdf"
    xml_path.write_text("<xml />", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4")

    dados = [{"_arquivo_origem": str(xml_path), "N° Documento": "123"}]
    logger = _FakeLogger()

    with patch("modules.runner.garantir_schema_run_state"), patch(
        "modules.runner.garantir_schema_nfse_notas"
    ), patch("modules.runner.upsert_state"), patch(
        "modules.runner.NFSeXMLConverterComAPI",
        return_value=_FakeConverter(dados),
    ), patch(
        "modules.runner.executar_fluxo_nfse_playwright",
        return_value=(True, 1, False, None),
    ), patch(
        "modules.runner.distribuir_por_competencia",
        return_value={
            "xml": [str(xml_path)],
            "pdf": [str(pdf_path)],
            "xml_encontrados_tmp": 1,
            "xml_duplicados": 0,
            "xml_falhas_distribuicao": 0,
        },
    ), patch(
        "modules.runner.detect_document_status_from_pdf_path",
        return_value="cancelada",
    ) as detect_mock, patch(
        "modules.runner.salvar_nota_nfse"
    ) as salvar_mock, patch(
        "modules.runner.atualizar_planilha_incremental",
        return_value=(0, 1),
    ):
        resultados = run_processing(cfg, logger=logger)

    detect_mock.assert_called_once_with(str(pdf_path))
    salvar_mock.assert_called_once()
    assert salvar_mock.call_args.kwargs["status_documental_pdf"] == "cancelada"
    assert resultados[0]["status"] == "ok"


def test_run_processing_nao_quebra_quando_pdf_nao_e_encontrado(tmp_path):
    cfg = _build_cfg(tmp_path)
    xml_dir = tmp_path / "saida" / "cert-a" / "2026" / "04" / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    xml_path = xml_dir / "Prestador NFS-e 123_1.xml"
    xml_path.write_text("<xml />", encoding="utf-8")

    dados = [{"_arquivo_origem": str(xml_path), "N° Documento": "123"}]
    logger = _FakeLogger()

    with patch("modules.runner.garantir_schema_run_state"), patch(
        "modules.runner.garantir_schema_nfse_notas"
    ), patch("modules.runner.upsert_state"), patch(
        "modules.runner.NFSeXMLConverterComAPI",
        return_value=_FakeConverter(dados),
    ), patch(
        "modules.runner.executar_fluxo_nfse_playwright",
        return_value=(True, 1, False, None),
    ), patch(
        "modules.runner.distribuir_por_competencia",
        return_value={
            "xml": [str(xml_path)],
            "pdf": [],
            "xml_encontrados_tmp": 1,
            "xml_duplicados": 0,
            "xml_falhas_distribuicao": 0,
        },
    ), patch(
        "modules.runner.detect_document_status_from_pdf_path",
        return_value=None,
    ) as detect_mock, patch(
        "modules.runner.salvar_nota_nfse"
    ) as salvar_mock, patch(
        "modules.runner.atualizar_planilha_incremental",
        return_value=(0, 1),
    ):
        resultados = run_processing(cfg, logger=logger)

    detect_mock.assert_called_once_with(None)
    salvar_mock.assert_called_once()
    assert salvar_mock.call_args.kwargs["status_documental_pdf"] is None
    assert resultados[0]["status"] == "ok"
