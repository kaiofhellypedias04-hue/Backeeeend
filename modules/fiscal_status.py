from __future__ import annotations

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