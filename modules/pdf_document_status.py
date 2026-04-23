from __future__ import annotations

import logging
import unicodedata
from pathlib import Path
from typing import Optional

import PyPDF2


DOCUMENT_STATUS_CANCELADA = "cancelada"
DOCUMENT_STATUS_SUBSTITUIDA = "substituida"
DOCUMENT_STATUS_NORMAL = "normal"
DOCUMENT_STATUS_VALUES = {
    DOCUMENT_STATUS_CANCELADA,
    DOCUMENT_STATUS_SUBSTITUIDA,
    DOCUMENT_STATUS_NORMAL,
}


logger = logging.getLogger(__name__)


def _normalize_pdf_text(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return " ".join(normalized.lower().split())


def classify_pdf_document_status(text: str) -> Optional[str]:
    normalized = _normalize_pdf_text(text)
    if not normalized:
        return None
    if "cancelada" in normalized:
        return DOCUMENT_STATUS_CANCELADA
    if "substituida" in normalized:
        return DOCUMENT_STATUS_SUBSTITUIDA
    return DOCUMENT_STATUS_NORMAL


def extract_pdf_text(pdf_path: str) -> str:
    with open(pdf_path, "rb") as file_obj:
        reader = PyPDF2.PdfReader(file_obj)
        return "\n".join((page.extract_text() or "") for page in reader.pages)


def detect_document_status_from_pdf_path(pdf_path: str | None) -> Optional[str]:
    if not pdf_path:
        logger.info("PDF nao encontrado para classificacao documental | pdf_path=%s | exists=false", pdf_path)
        return None

    path = Path(pdf_path)
    if not path.exists() or not path.is_file():
        logger.info("PDF nao encontrado para classificacao documental | pdf_path=%s | exists=false", pdf_path)
        return None

    try:
        text = extract_pdf_text(str(path))
        if not text.strip():
            logger.info("Falha de leitura/classificacao do PDF documental | pdf_path=%s | text_empty=true", pdf_path)
            return None

        status = classify_pdf_document_status(text)
        if status is not None:
            logger.info("Status documental do PDF calculado | pdf_path=%s | status=%s", pdf_path, status)
        return status
    except Exception:
        return None
