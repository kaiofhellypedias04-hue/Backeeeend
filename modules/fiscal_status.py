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


def _normalize_tax_regime_text(value: Any) -> str:
    txt = str(value or "").strip().lower()
    txt = " ".join(txt.split())
    txt = unicodedata.normalize("NFD", txt)
    return "".join(ch for ch in txt if unicodedata.category(ch) != "Mn")


def _classify_tax_regime(value: Any) -> str:
    normalized = _normalize_tax_regime_text(value)
    compact = normalized.replace(".", "").replace("-", " ")
    compact = " ".join(compact.split())
    if not compact:
        return ""
    if "nao optante" in compact:
        return "NAO_OPTANTE"
    if "mei" in compact:
        return "MEI"
    if "optante" in compact or "simples" in compact:
        return "OPTANTE"
    return ""


def resolve_effective_tax_regime(simples_xml: Any = None, consulta_simples_api: Any = None) -> str:
    xml_regime = _classify_tax_regime(simples_xml)
    api_regime = _classify_tax_regime(consulta_simples_api)

    # A API tem precedência quando informa "não optante".
    if api_regime == "NAO_OPTANTE":
        return api_regime
    # Mantém MEI como caso especial, inclusive quando a API vier apenas como "optante".
    if api_regime == "MEI" or xml_regime == "MEI":
        return "MEI"
    if api_regime:
        return api_regime
    return xml_regime


def build_base_calculation_alert(
    valor_bc: float | None,
    valor_total: float | None,
    tolerance: float = 0.01,
    *,
    simples_xml: Any = None,
    consulta_simples_api: Any = None,
    codigo_servico: Any = None,
) -> str | None:
    if valor_bc is None or valor_total is None:
        return None
    if valor_total <= tolerance or abs(valor_bc) > tolerance:
        return None

    regime = resolve_effective_tax_regime(
        simples_xml=simples_xml,
        consulta_simples_api=consulta_simples_api,
    )
    if regime == "OPTANTE":
        return "BASE ZERADA: base de calculo zerada para Optante Simples. Verificar."
    if regime == "NAO_OPTANTE":
        return "BASE ZERADA: base de calculo zerada para Nao Optante. Verificar."
    return None


def compute_base_calculation_status(
    valor_bc: float | None,
    valor_total: float | None,
    tolerance: float = 0.01,
    *,
    simples_xml: Any = None,
    consulta_simples_api: Any = None,
    codigo_servico: Any = None,
) -> str:
    if valor_bc is None:
        return "ausente"
    if valor_bc < -tolerance:
        return "divergente"
    if valor_total is not None and valor_bc - valor_total > tolerance:
        return "divergente"
    if valor_total is not None and valor_total > tolerance and abs(valor_bc) <= tolerance:
        regime = resolve_effective_tax_regime(
            simples_xml=simples_xml,
            consulta_simples_api=consulta_simples_api,
        )
        if regime in {"OPTANTE", "NAO_OPTANTE"}:
            return "divergente"
    return "ok"


def normalize_text_for_suffix_match(value: Any) -> str:
    txt = str(value or "").strip().lower()
    txt = " ".join(txt.split())
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(ch for ch in txt if unicodedata.category(ch) != "Mn")
    return txt


def _last_non_empty_pipe_segment(value: Any) -> str:
    parts = str(value or "").split("|")
    for part in reversed(parts):
        chunk = part.strip()
        if chunk:
            return chunk
    return ""


def is_alertas_fiscais_final_segment_correto(alertas_fiscais: Any) -> bool:
    last_segment = _last_non_empty_pipe_segment(alertas_fiscais)
    if not last_segment:
        return False
    normalized = normalize_text_for_suffix_match(last_segment)
    return normalized.endswith("correto") or normalized.endswith("correta")


def is_observacao_fiscal_final_segment_correto(observacao_fiscal: Any) -> bool:
    last_segment = _last_non_empty_pipe_segment(observacao_fiscal)
    if not last_segment:
        return False
    normalized = normalize_text_for_suffix_match(last_segment)
    return normalized.endswith("correto") or normalized.endswith("correta")


def compute_final_queue_status(payload: dict[str, Any], fields: Iterable[str] = FINAL_STATUS_FIELDS) -> str:
    manual = normalize_status_value(payload.get("status_fila_manual"))
    if manual:
        return manual
    observacao_norm = normalize_text_for_suffix_match(payload.get("observacao_interna"))
    if "diverg" in observacao_norm:
        return "divergente"
    alertas_norm = normalize_text_for_suffix_match(payload.get("alertas_fiscais"))
    if "correto" in alertas_norm or "correta" in alertas_norm:
        return "correta"
    if alertas_norm:
        return "divergente"
    return compute_final_note_status(payload, fields=fields)


def _build_sql_final_segment_expr(alias: str, field_name: str) -> str:
    last_segment_expr = f"""(
        SELECT BTRIM(part)
        FROM unnest(regexp_split_to_array(COALESCE({alias}.{field_name}, ''), '\\\\|')) WITH ORDINALITY AS parts(part, ord)
        WHERE BTRIM(part) <> ''
        ORDER BY ord DESC
        LIMIT 1
    )"""
    return last_segment_expr


def build_sql_observacao_final_segment_correto_expr(alias: str = "n") -> str:
    last_segment_expr = _build_sql_final_segment_expr(alias, "observacao_interna")
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
    return f"({normalized_expr} LIKE '%%correto' OR {normalized_expr} LIKE '%%correta')"


def build_sql_alertas_final_segment_correto_expr(alias: str = "n") -> str:
    last_segment_expr = _build_sql_final_segment_expr(alias, "alertas_fiscais")
    normalized_expr = (
        "LOWER("
        "REGEXP_REPLACE("
        "TRANSLATE("
        f"COALESCE({last_segment_expr}, ''), "
        "'ÃÃ€Ã‚ÃƒÃ„Ã¡Ã Ã¢Ã£Ã¤Ã‰ÃˆÃŠÃ‹Ã©Ã¨ÃªÃ«ÃÃŒÃŽÃÃ­Ã¬Ã®Ã¯Ã“Ã’Ã”Ã•Ã–Ã³Ã²Ã´ÃµÃ¶ÃšÃ™Ã›ÃœÃºÃ¹Ã»Ã¼Ã‡Ã§', "
        "'AAAAAaaaaaEEEEeeeeIIIIiiiiOOOOOoooooUUUUuuuuCc'"
        "), "
        "'\\s+', ' ', 'g'"
        ")"
        ")"
    )
    return f"({normalized_expr} LIKE '%%correto' OR {normalized_expr} LIKE '%%correta')"


def build_sql_queue_status_expr(alias: str = "n", note_status_expr: str | None = None) -> str:
    note_expr = note_status_expr or build_sql_status_expr(alias)
    manual_expr = f"NULLIF(BTRIM({alias}.status_fila_manual), '')"
    observacao_diverg_expr = f"LOWER(COALESCE({alias}.observacao_interna, '')) LIKE '%%diverg%%'"
    alertas_ok_expr = (
        f"LOWER(COALESCE({alias}.alertas_fiscais, '')) LIKE '%%correto%%' "
        f"OR LOWER(COALESCE({alias}.alertas_fiscais, '')) LIKE '%%correta%%'"
    )
    alert_real_expr = f"NULLIF(BTRIM(COALESCE({alias}.alertas_fiscais, '')), '') IS NOT NULL"
    return f"""(
    CASE
      WHEN {manual_expr} IS NOT NULL THEN {manual_expr}
      WHEN {observacao_diverg_expr} THEN 'divergente'
      WHEN {alertas_ok_expr} THEN 'correta'
      WHEN {alert_real_expr} THEN 'divergente'
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
