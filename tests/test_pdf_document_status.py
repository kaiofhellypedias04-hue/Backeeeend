import unittest
from unittest.mock import patch

from modules.pdf_document_status import (
    classify_pdf_document_status,
    detect_document_status_from_pdf_path,
)


class PdfDocumentStatusTests(unittest.TestCase):
    def test_classifica_cancelada(self):
        self.assertEqual(classify_pdf_document_status("CANCELADA\nDANFSe v1.0"), "cancelada")

    def test_classifica_substituida_com_acento(self):
        self.assertEqual(classify_pdf_document_status("SUBSTITUÍDA\nDANFSe v1.0"), "substituida")

    def test_classifica_normal_quando_nao_ha_marcador(self):
        self.assertEqual(classify_pdf_document_status("DANFSe v1.0\nDocumento Auxiliar"), "normal")

    def test_detecta_pdf_inexistente_sem_quebrar(self):
        self.assertIsNone(detect_document_status_from_pdf_path("C:/arquivo/inexistente.pdf"))

    @patch("modules.pdf_document_status.extract_pdf_text", side_effect=RuntimeError("falha"))
    @patch("pathlib.Path.is_file", return_value=True)
    @patch("pathlib.Path.exists", return_value=True)
    def test_falha_na_leitura_retorna_none(self, *_mocks):
        self.assertIsNone(detect_document_status_from_pdf_path("C:/tmp/nota.pdf"))

