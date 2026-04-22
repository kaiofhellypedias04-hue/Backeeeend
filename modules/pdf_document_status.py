from __future__ import annotations

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
        return None

    path = Path(pdf_path)
    if not path.exists() or not path.is_file():
        return None

    try:
        return classify_pdf_document_status(extract_pdf_text(str(path)))
    except Exception:
        return None

