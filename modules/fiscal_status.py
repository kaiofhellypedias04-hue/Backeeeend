from __future__ import annotations

import unicodedata
from typing import Any, Iterable


OK_VALUES = {"", "ok", "correto", "sem divergencia", "sem divergência"}
DIVERGENT_VALUES = {"divergente", "ausente", "erro", "inconsistente"}

FINAL_STATUS_FIELDS = (
    "status_csrf",
    "status_irrf",
    "status_inss",
    "status_base_calculo",
    "status_valor_liquido",
)


def normalize_status_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def is_divergent_status_value(value: Any) -> bool:
    normalized = normalize_status_value(value)
    if not normalized:
        return False
    if normalized in DIVERGENT_VALUES:
        return True
    return normalized not in OK_VALUES


def compute_final_note_status(payload: dict[str, Any], fields: Iterable[str] = FINAL_STATUS_FIELDS) -> str:
    for field in fields:
        if is_divergent_status_value(payload.get(field)):
            return "divergente"
    return "correta"


def compute_base_calculation_status(
    valor_bc: float | None,
    valor_total: float | None,
    tolerance: float = 0.01,
) -> str:
    if valor_bc is None:
        return "ausente"
    if valor_bc < -tolerance:
        return "divergente"
    if valor_total is not None and valor_bc - valor_total > tolerance:
        return "divergente"
    return "ok"


def normalize_text_for_suffix_match(value: Any) -> str:
    txt = str(value or "").strip().lower()
    txt = " ".join(txt.split())
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(ch for ch in txt if unicodedata.category(ch) != "Mn")
    return txt


def _last_non_empty_alert_segment(alertas_fiscais: Any) -> str:
    parts = str(alertas_fiscais or "").split("|")
    for part in reversed(parts):
        chunk = part.strip()
        if chunk:
            return chunk
    return ""


def is_alertas_fiscais_final_segment_correto(alertas_fiscais: Any) -> bool:
    last_segment = _last_non_empty_alert_segment(alertas_fiscais)
    if not last_segment:
        return False
    normalized = normalize_text_for_suffix_match(last_segment)
    return normalized.endswith("correto") or normalized.endswith("correta")


def compute_final_queue_status(payload: dict[str, Any], fields: Iterable[str] = FINAL_STATUS_FIELDS) -> str:
    manual = normalize_status_value(payload.get("status_fila_manual"))
    if manual:
        return manual
    if is_alertas_fiscais_final_segment_correto(payload.get("alertas_fiscais")):
        return "correta"
    return compute_final_note_status(payload, fields=fields)


def build_sql_alertas_final_segment_correto_expr(alias: str = "n") -> str:
    last_segment_expr = f"""(
        SELECT BTRIM(part)
        FROM unnest(regexp_split_to_array(COALESCE({alias}.alertas_fiscais, ''), '\\\\|')) WITH ORDINALITY AS parts(part, ord)
        WHERE BTRIM(part) <> ''
        ORDER BY ord DESC
        LIMIT 1
    )"""
    normalized_expr = (
        "LOWER("
        "REGEXP_REPLACE("
        "TRANSLATE("
        f"COALESCE({last_segment_expr}, ''), "
        "'ÁÀÂÃÄáàâãäÉÈÊËéèêëÍÌÎÏíìîïÓÒÔÕÖóòôõöÚÙÛÜúùûüÇç', "
        "'AAAAAaaaaaEEEEeeeeIIIIiiiiOOOOOoooooUUUUuuuuCc'"
        "), "
        "'\\s+', ' ', 'g'"
        ")"
        ")"
    )
    return f"({normalized_expr} LIKE '%correto' OR {normalized_expr} LIKE '%correta')"


def build_sql_queue_status_expr(alias: str = "n", note_status_expr: str | None = None) -> str:
    note_expr = note_status_expr or build_sql_status_expr(alias)
    manual_expr = f"NULLIF(BTRIM({alias}.status_fila_manual), '')"
    alert_ok_expr = build_sql_alertas_final_segment_correto_expr(alias)
    return f"""(
    CASE
      WHEN {manual_expr} IS NOT NULL THEN {manual_expr}
      WHEN {alert_ok_expr} THEN 'correta'
      ELSE {note_expr}
    END
)"""


def build_sql_status_expr(alias: str = "n") -> str:
    ok_values_sql = ", ".join(f"'{value}'" for value in sorted(OK_VALUES))
    conditions = [
        f"LOWER(COALESCE({alias}.{field}, 'ok')) IN ({ok_values_sql})"
        for field in FINAL_STATUS_FIELDS
    ]
    return """(
    CASE
      WHEN {conditions}
      THEN 'correta'
      ELSE 'divergente'
    END
)""".format(conditions="\n       AND ".join(conditions))
