from __future__ import annotations

from pathlib import Path
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

from .db import get_conn
from .nfse_keys import gerar_chave_nfse
from .fiscal_status import build_sql_status_expr


STATUS_EXPR = build_sql_status_expr("n")

STATUS_FILA_EXPR = f"""COALESCE(NULLIF(n.status_fila_manual, ''), {STATUS_EXPR})"""
STATUS_FILA_FILTER_EXPR = f"LOWER(BTRIM(COALESCE({STATUS_FILA_EXPR}, '')))"


def garantir_schema_nfse_notas():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nfse_notas (
          id BIGSERIAL PRIMARY KEY,
          cert_alias TEXT NOT NULL,
          processo_id UUID,
          chave_nfse TEXT NOT NULL,
          numero_documento TEXT,
          competencia TEXT,
          data_emissao DATE,
          municipio TEXT,
          cnpj_prestador TEXT,
          razao_social TEXT,
          valor_total NUMERIC,
          valor_bc NUMERIC,
          valor_liquido NUMERIC,
          valor_liquido_correto NUMERIC,
          csrf NUMERIC,
          irrf NUMERIC,
          percentual_irrf NUMERIC,
          inss NUMERIC,
          iss NUMERIC,
          retencao_csrf TEXT,
          incidencia_iss TEXT,
          data_pagamento TEXT,
          codigo_servico TEXT,
          descricao_servico TEXT,
          codigo_nbs TEXT,
          codigo_cnae TEXT,
          descricao_cnae TEXT,
          simples_xml TEXT,
          consulta_simples_api TEXT,
          status_simples_nacional TEXT,
          status_csrf TEXT,
          status_irrf TEXT,
          status_inss TEXT,
          status_base_calculo TEXT,
          status_valor_liquido TEXT,
          campos_ausentes_xml TEXT,
          alertas_fiscais TEXT,
          irrf_calculado NUMERIC,
          csrf_calculado NUMERIC,
          iss_calculado NUMERIC,
          observacao_interna TEXT,
          status_fila_manual TEXT,
          prioridade_manual TEXT,
          responsavel TEXT,
          tipo_nota TEXT,
          parte_exibicao_nome TEXT,
          parte_exibicao_doc TEXT,
          parte_exibicao_tipo TEXT,
          dados_completos JSONB NOT NULL,
          arquivo_origem TEXT,
          created_at TIMESTAMP NOT NULL DEFAULT now(),
          updated_at TIMESTAMP NOT NULL DEFAULT now()
        );
        """)
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS cert_alias TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS processo_id UUID")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS chave_nfse TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS tipo_nota TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS parte_exibicao_nome TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS parte_exibicao_doc TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS parte_exibicao_tipo TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS valor_liquido_correto NUMERIC")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS status_valor_liquido TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS campos_ausentes_xml TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS irrf_calculado NUMERIC")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS csrf_calculado NUMERIC")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS iss_calculado NUMERIC")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS observacao_interna TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS status_fila_manual TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS prioridade_manual TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS responsavel TEXT")
        conn.execute("ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT now()")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_nfse_notas_cert_chave ON nfse_notas (cert_alias, chave_nfse)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nfse_notas_processo ON nfse_notas (processo_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nfse_notas_tipo_nota ON nfse_notas (tipo_nota)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nfse_notas_status_fila_manual ON nfse_notas (status_fila_manual)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nfse_notas_responsavel ON nfse_notas (responsavel)")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nfse_processo_notas (
          processo_id UUID NOT NULL,
          nota_id BIGINT NOT NULL REFERENCES nfse_notas(id) ON DELETE CASCADE,
          created_at TIMESTAMP NOT NULL DEFAULT now(),
          PRIMARY KEY (processo_id, nota_id)
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nfse_processo_notas_nota ON nfse_processo_notas (nota_id)")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nfse_regras_atribuicao (
          id BIGSERIAL PRIMARY KEY,
          campo TEXT NOT NULL,
          operador TEXT NOT NULL,
          valor TEXT NOT NULL,
          responsavel TEXT NOT NULL,
          prioridade INTEGER NOT NULL DEFAULT 100,
          ativo BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMP NOT NULL DEFAULT now(),
          updated_at TIMESTAMP NOT NULL DEFAULT now()
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nfse_regras_atribuicao_ativo ON nfse_regras_atribuicao (ativo)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nfse_regras_atribuicao_prioridade ON nfse_regras_atribuicao (prioridade)")


def _to_text_alertas(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, list):
        partes = [str(item).strip() for item in value if item is not None and str(item).strip()]
        return " | ".join(partes) if partes else None
    if isinstance(value, dict):
        partes = []
        for k, v in value.items():
            k_txt = str(k).strip()
            v_txt = str(v).strip() if v is not None else ""
            if v_txt:
                partes.append(f"{k_txt}: {v_txt}" if k_txt else v_txt)
        return " | ".join(partes) if partes else None
    txt = str(value).strip()
    return txt or None


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    txt = str(value).strip()
    return txt or None


def _normalize_rule_text(value: Any) -> str:
    txt = str(value or "").strip().lower()
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(ch for ch in txt if unicodedata.category(ch) != "Mn")
    return txt


def _extract_rule_field_value(campo: str, cert_alias: str, data: dict) -> str:
    mapping = {
        "descricao_servico": data.get("Descrição do Serviço") or data.get("descricao_servico") or "",
        "item_nfse": data.get("Descrição do Serviço") or data.get("descricao_servico") or "",
        "razao_social": data.get("Razão Social") or data.get("razao_social") or "",
        "fornecedor": data.get("Razão Social") or data.get("razao_social") or "",
        "parte_exibicao_nome": data.get("Razão Social") or data.get("razao_social") or "",
        "cert_alias": cert_alias or "",
        "codigo_servico": data.get("Código de serviço") or data.get("codigo_servico") or "",
    }
    return str(mapping.get(campo, "") or "")


def _rule_matches(campo_valor: str, operador: str, valor: str) -> bool:
    source = _normalize_rule_text(campo_valor)
    target = _normalize_rule_text(valor)
    if not target:
        return False
    if operador == "equals":
        return source == target
    if operador == "starts_with":
        return source.startswith(target)
    return target in source


def listar_regras_atribuicao() -> List[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, campo, operador, valor, responsavel, prioridade, ativo, created_at, updated_at
            FROM nfse_regras_atribuicao
            ORDER BY prioridade ASC, id ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _listar_regras_ativas() -> List[dict]:
    return [r for r in listar_regras_atribuicao() if r.get("ativo")]


def criar_regra_atribuicao(campo: str, operador: str, valor: str, responsavel: str, prioridade: int = 100, ativo: bool = True) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO nfse_regras_atribuicao (campo, operador, valor, responsavel, prioridade, ativo, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            RETURNING id, campo, operador, valor, responsavel, prioridade, ativo, created_at, updated_at
            """,
            (_clean_text(campo), _clean_text(operador), _clean_text(valor), _clean_text(responsavel), prioridade, bool(ativo)),
        ).fetchone()
    return dict(row)


def atualizar_regra_atribuicao(regra_id: int, campo: str, operador: str, valor: str, responsavel: str, prioridade: int, ativo: bool) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            """
            UPDATE nfse_regras_atribuicao
            SET campo = %s,
                operador = %s,
                valor = %s,
                responsavel = %s,
                prioridade = %s,
                ativo = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING id, campo, operador, valor, responsavel, prioridade, ativo, created_at, updated_at
            """,
            (_clean_text(campo), _clean_text(operador), _clean_text(valor), _clean_text(responsavel), prioridade, bool(ativo), regra_id),
        ).fetchone()
    return dict(row) if row else None


def excluir_regra_atribuicao(regra_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("DELETE FROM nfse_regras_atribuicao WHERE id = %s RETURNING id", (regra_id,)).fetchone()
    return bool(row)


def resolver_responsavel_automatico(cert_alias: str, data: dict) -> Optional[str]:
    regras = _listar_regras_ativas()
    for regra in regras:
        campo = str(regra.get("campo") or "")
        operador = str(regra.get("operador") or "contains")
        valor = str(regra.get("valor") or "")
        campo_valor = _extract_rule_field_value(campo, cert_alias, data)
        if _rule_matches(campo_valor, operador, valor):
            return _clean_text(regra.get("responsavel"))
    return None


def reaplicar_regras_atribuicao(only_empty: bool = True) -> int:
    regras = _listar_regras_ativas()
    if not regras:
        return 0

    with get_conn() as conn:
        sql = """
            SELECT id, cert_alias, responsavel, dados_completos
            FROM nfse_notas
        """
        if only_empty:
            sql += " WHERE COALESCE(TRIM(responsavel), '') = ''"
        rows = conn.execute(sql).fetchall()

        atualizados = 0
        for row in rows:
            data = row["dados_completos"] or {}
            novo_responsavel = None
            for regra in regras:
                campo_valor = _extract_rule_field_value(str(regra.get("campo") or ""), str(row["cert_alias"] or ""), data)
                if _rule_matches(campo_valor, str(regra.get("operador") or "contains"), str(regra.get("valor") or "")):
                    novo_responsavel = _clean_text(regra.get("responsavel"))
                    break
            if novo_responsavel:
                conn.execute(
                    "UPDATE nfse_notas SET responsavel = %s, updated_at = now() WHERE id = %s",
                    (novo_responsavel, row["id"]),
                )
                atualizados += 1
    return atualizados


def _extract_row_id(row: Any) -> Optional[int]:
    if not row:
        return None
    try:
        value = row["id"]
        return int(value) if value is not None else None
    except Exception:
        pass
    try:
        value = row[0]
        return int(value) if value is not None else None
    except Exception:
        pass
    return None


def _extract_total(total_row: Any) -> int:
    if not total_row:
        return 0
    for key in ("total", 0):
        try:
            value = total_row[key]
            return int(value or 0)
        except Exception:
            pass
    try:
        return int(dict(total_row).get("total", 0) or 0)
    except Exception:
        return 0


def _to_decimal(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None

    if isinstance(v, (int, float)):
        return float(v)

    s = str(v).strip()
    s = re.sub(r"[R$\s]+", "", s)

    if not s:
        return None

    try:
        if "," in s:
            s_clean = s.replace(".", "").replace(",", ".")
            return float(s_clean)

        if "." in s:
            partes = s.split(".")
            if len(partes) > 2 and all(len(p) == 3 for p in partes[1:]):
                return float(s.replace(".", ""))
            last = partes[-1]
            if len(partes) == 2 and len(last) == 3 and last.isdigit():
                return float(s.replace(".", ""))
            return float(s)

        return float(s)
    except Exception:
        return None


def _status_compare(xml_value: Optional[float], expected_value: Optional[float], tolerance: float = 0.01) -> str:
    if xml_value is None:
        return "ausente"
    if expected_value is None:
        return "ok"
    return "ok" if abs(xml_value - expected_value) <= tolerance else "divergente"


def _build_campos_ausentes_xml(data: dict) -> Optional[str]:
    campos_obrigatorios = [
        "N° Documento",
        "Competência",
        "Data de Emissão",
        "Município",
        "CNPJ/CPF",
        "Razão Social",
        "Valor Total",
        "Valor Líquido",
        "Valor B/C",
    ]
    faltantes = [campo for campo in campos_obrigatorios if data.get(campo) in (None, "")]
    return " | ".join(faltantes) if faltantes else None


def salvar_nota_nfse(cert_alias: str, processo_id: str | None, data: dict, arquivo_origem: str | None = None) -> str:
    tipo_nota = "tomados"

    if processo_id:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT tipo_nota FROM nfse_processos WHERE id = %s",
                (processo_id,)
            ).fetchone()
            if row:
                try:
                    tipo_nota = row["tipo_nota"] or tipo_nota
                except Exception:
                    try:
                        tipo_nota = row[0] or tipo_nota
                    except Exception:
                        pass

    nome_raw = data.get("Razão Social") or "—"
    doc_raw = data.get("CNPJ/CPF") or "—"
    parte_exibicao_tipo = "tomador" if tipo_nota == "tomados" else "prestador"
    parte_exibicao_nome = nome_raw
    parte_exibicao_doc = doc_raw

    chave_nfse = gerar_chave_nfse(data)

    valor_total = _to_decimal(data.get("Valor Total"))
    valor_bc = _to_decimal(data.get("Valor B/C"))
    valor_liquido = _to_decimal(data.get("Valor Líquido"))
    csrf = _to_decimal(data.get("CSRF"))
    irrf = _to_decimal(data.get("IRRF"))
    inss = _to_decimal(data.get("INSS"))
    iss = _to_decimal(data.get("ISS"))

    valor_liquido_correto = None
    if valor_total is not None:
        valor_liquido_correto = (
            valor_total
            - (csrf or 0)
            - (irrf or 0)
            - (inss or 0)
            - (iss or 0)
        )

    status_valor_liquido = _status_compare(valor_liquido, valor_liquido_correto)
    status_base_calculo = _status_compare(valor_bc, valor_total)
    campos_ausentes_xml = _build_campos_ausentes_xml(data)

    alertas_fiscais_txt = _to_text_alertas(data.get("Alertas Fiscais"))
    irrf_calculado = _to_decimal(data.get("_IRRF_Calculado"))
    csrf_calculado = _to_decimal(data.get("_CSRF_Calculado"))
    iss_calculado = _to_decimal(data.get("_ISS_Calculado"))
    responsavel_automatico = resolver_responsavel_automatico(cert_alias, data)

    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO nfse_notas (
              cert_alias, processo_id, chave_nfse, tipo_nota, parte_exibicao_nome,
              parte_exibicao_doc, parte_exibicao_tipo,
              numero_documento, competencia, data_emissao, municipio,
              cnpj_prestador, razao_social,
              valor_total, valor_bc, valor_liquido, valor_liquido_correto,
              csrf, irrf, percentual_irrf, inss, iss,
              incidencia_iss, data_pagamento,
              codigo_servico, descricao_servico, codigo_nbs, codigo_cnae, descricao_cnae,
              simples_xml, consulta_simples_api,
              status_simples_nacional, status_csrf, status_irrf, status_inss, status_base_calculo, status_valor_liquido,
              campos_ausentes_xml, alertas_fiscais,
              irrf_calculado, csrf_calculado, iss_calculado,
              responsavel,
              dados_completos, arquivo_origem,
              updated_at
            )
            VALUES (
              %s,%s,%s,%s,%s,%s,%s,
              %s,%s,%s,%s,
              %s,%s,
              %s,%s,%s,%s,
              %s,%s,%s,%s,%s,
              %s,%s,
              %s,%s,%s,%s,%s,
              %s,%s,
              %s,%s,%s,%s,%s,%s,
              %s,%s,
              %s,%s,%s,
              %s,
              %s,%s,
              now()
            )
            ON CONFLICT (cert_alias, chave_nfse)
            DO UPDATE SET
              tipo_nota = EXCLUDED.tipo_nota,
              parte_exibicao_nome = EXCLUDED.parte_exibicao_nome,
              parte_exibicao_doc = EXCLUDED.parte_exibicao_doc,
              parte_exibicao_tipo = EXCLUDED.parte_exibicao_tipo,
              numero_documento = EXCLUDED.numero_documento,
              competencia = EXCLUDED.competencia,
              data_emissao = EXCLUDED.data_emissao,
              municipio = EXCLUDED.municipio,
              cnpj_prestador = EXCLUDED.cnpj_prestador,
              razao_social = EXCLUDED.razao_social,
              valor_total = EXCLUDED.valor_total,
              valor_bc = EXCLUDED.valor_bc,
              valor_liquido = EXCLUDED.valor_liquido,
              valor_liquido_correto = EXCLUDED.valor_liquido_correto,
              csrf = EXCLUDED.csrf,
              irrf = EXCLUDED.irrf,
              percentual_irrf = EXCLUDED.percentual_irrf,
              inss = EXCLUDED.inss,
              iss = EXCLUDED.iss,
              incidencia_iss = EXCLUDED.incidencia_iss,
              data_pagamento = EXCLUDED.data_pagamento,
              codigo_servico = EXCLUDED.codigo_servico,
              descricao_servico = EXCLUDED.descricao_servico,
              codigo_nbs = EXCLUDED.codigo_nbs,
              codigo_cnae = EXCLUDED.codigo_cnae,
              descricao_cnae = EXCLUDED.descricao_cnae,
              simples_xml = EXCLUDED.simples_xml,
              consulta_simples_api = EXCLUDED.consulta_simples_api,
              status_simples_nacional = EXCLUDED.status_simples_nacional,
              status_csrf = EXCLUDED.status_csrf,
              status_irrf = EXCLUDED.status_irrf,
              status_inss = EXCLUDED.status_inss,
              status_base_calculo = EXCLUDED.status_base_calculo,
              status_valor_liquido = EXCLUDED.status_valor_liquido,
              campos_ausentes_xml = EXCLUDED.campos_ausentes_xml,
              alertas_fiscais = EXCLUDED.alertas_fiscais,
              irrf_calculado = EXCLUDED.irrf_calculado,
              csrf_calculado = EXCLUDED.csrf_calculado,
              iss_calculado = EXCLUDED.iss_calculado,
              responsavel = COALESCE(NULLIF(nfse_notas.responsavel, ''), EXCLUDED.responsavel),
              dados_completos = EXCLUDED.dados_completos,
              arquivo_origem = COALESCE(EXCLUDED.arquivo_origem, nfse_notas.arquivo_origem),
              updated_at = now()
            RETURNING id
            """,
            (
                cert_alias, processo_id, chave_nfse, tipo_nota,
                parte_exibicao_nome, parte_exibicao_doc, parte_exibicao_tipo,
                data.get("N° Documento"), data.get("Competência"),
                data.get("Data de Emissão"), data.get("Município"),
                data.get("CNPJ/CPF"), data.get("Razão Social"),
                valor_total, valor_bc, valor_liquido, valor_liquido_correto,
                csrf, irrf, _to_decimal(data.get("Percentual IRRF")), inss, iss,
                data.get("Incidência do ISS"), data.get("Data do pagamento"),
                data.get("Código de serviço"), data.get("Descrição do Serviço"),
                data.get("Código NBS"), data.get("Código CNAE"), data.get("Descrição CNAE"),
                data.get("Simples Nacional / XML"), data.get("Consulta Simples API"),
                data.get("Status Simples Nacional"), data.get("Status CSRF"),
                data.get("Status IRRF"), data.get("Status INSS"),
                status_base_calculo, status_valor_liquido,
                campos_ausentes_xml, alertas_fiscais_txt,
                irrf_calculado, csrf_calculado, iss_calculado,
                responsavel_automatico,
                Jsonb(data), arquivo_origem,
            ),
        ).fetchone()

        nota_id = _extract_row_id(row)

        if row and nota_id is None:
            raise RuntimeError(f"RETURNING id veio em formato inesperado: {type(row)} / {row}")

        if processo_id and nota_id is not None:
            conn.execute(
                """
                INSERT INTO nfse_processo_notas (processo_id, nota_id)
                VALUES (%s, %s)
                ON CONFLICT (processo_id, nota_id) DO NOTHING
                """,
                (processo_id, nota_id),
            )

    return chave_nfse


def atualizar_nota_campos_editaveis(
    nota_id: int,
    valor_liquido_correto: Optional[float],
    alertas_fiscais: Optional[str],
    observacao_interna: Optional[str] = None,
    status_fila_manual: Optional[str] = None,
    prioridade_manual: Optional[str] = None,
    responsavel: Optional[str] = None,
) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT valor_liquido, valor_liquido_correto, alertas_fiscais,
                   observacao_interna, status_fila_manual, prioridade_manual, responsavel
            FROM nfse_notas
            WHERE id = %s
            """,
            (nota_id,)
        ).fetchone()

        if not row:
            return False

        novo_correto = valor_liquido_correto if valor_liquido_correto is not None else row["valor_liquido_correto"]
        valor_liquido = row["valor_liquido"]
        novo_status = _status_compare(valor_liquido, novo_correto)
        novos_alertas = alertas_fiscais if alertas_fiscais is not None else row["alertas_fiscais"]
        nova_obs = observacao_interna if observacao_interna is not None else row["observacao_interna"]
        novo_status_fila_manual = _clean_text(status_fila_manual) if status_fila_manual is not None else row["status_fila_manual"]
        nova_prioridade = _clean_text(prioridade_manual) if prioridade_manual is not None else row["prioridade_manual"]
        novo_responsavel = _clean_text(responsavel) if responsavel is not None else row["responsavel"]

        conn.execute(
            """
            UPDATE nfse_notas
            SET valor_liquido_correto = %s,
                alertas_fiscais = %s,
                observacao_interna = %s,
                status_fila_manual = %s,
                prioridade_manual = %s,
                responsavel = %s,
                status_valor_liquido = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (novo_correto, novos_alertas, nova_obs, novo_status_fila_manual, nova_prioridade, novo_responsavel, novo_status, nota_id),
        )
    return True


def _build_where(filters: Optional[dict], processo_id: Optional[str] = None) -> Tuple[str, List[Any]]:
    params: List[Any] = []
    where_clauses: List[str] = []

    if processo_id:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM nfse_processo_notas ppn WHERE ppn.nota_id = n.id AND ppn.processo_id = %s)"
        )
        params.append(processo_id)

    if filters:
        status = filters.get("status")
        if status:
            where_clauses.append(f"{STATUS_FILA_FILTER_EXPR} = LOWER(BTRIM(%s))")
            params.append(status)

        municipio = filters.get("municipio")
        if municipio:
            where_clauses.append("n.municipio ILIKE %s")
            params.append(f"%{municipio}%")

        cnpj_cpf = filters.get("cnpj_cpf")
        if cnpj_cpf:
            where_clauses.append("COALESCE(n.parte_exibicao_doc, n.cnpj_prestador, '') ILIKE %s")
            params.append(f"%{cnpj_cpf}%")

        competencia = filters.get("competencia")
        if competencia:
            where_clauses.append("n.competencia = %s")
            params.append(competencia)

        codigo_servico = filters.get("codigo_servico")
        if codigo_servico:
            where_clauses.append("n.codigo_servico = %s")
            params.append(codigo_servico)

        data_tipo = filters.get("data_tipo") or "entrada"
        data_inicio = filters.get("data_inicio")
        data_fim = filters.get("data_fim")
        if data_inicio:
            if data_tipo == "emissao":
                where_clauses.append("n.data_emissao >= %s")
            else:
                where_clauses.append("DATE(n.created_at) >= %s")
            params.append(data_inicio)
        if data_fim:
            if data_tipo == "emissao":
                where_clauses.append("n.data_emissao <= %s")
            else:
                where_clauses.append("DATE(n.created_at) <= %s")
            params.append(data_fim)

        cert_alias = filters.get("cert_alias")
        if cert_alias:
            where_clauses.append("n.cert_alias = %s")
            params.append(cert_alias)

        if filters.get("somente_divergentes"):
            where_clauses.append(f"{STATUS_FILA_FILTER_EXPR} = 'divergente'")

    where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    return where, params


def listar_notas_por_processo(
    processo_id: str,
    filters: Optional[dict] = None,
    page: int = 1,
    page_size: int = 20,
    order_by: str = "n.created_at DESC",
) -> tuple[List[dict], int]:
    offset = (page - 1) * page_size
    where, params = _build_where(filters, processo_id=processo_id)

    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT n.id,
                   %s::uuid as processo_id,
                   COALESCE(p.cert_alias, n.cert_alias) as certificado,
                   n.tipo_nota,
                   n.numero_documento,
                   n.competencia,
                   n.municipio,
                   n.chave_nfse as chave_acesso,
                   n.data_emissao,
                   COALESCE(n.parte_exibicao_doc, n.cnpj_prestador, '—') as cnpj_cpf,
                   COALESCE(n.parte_exibicao_nome, n.razao_social, '—') as razao_social,
                   COALESCE(n.parte_exibicao_tipo, CASE WHEN n.tipo_nota = 'tomados' THEN 'tomador' ELSE 'prestador' END, '') as parte_exibicao_tipo,
                   COALESCE(n.parte_exibicao_nome, n.razao_social, '—') as parte_exibicao_nome,
                   COALESCE(n.parte_exibicao_doc, n.cnpj_prestador, '—') as parte_exibicao_doc,
                   n.valor_total,
                   n.valor_bc as valor_base,
                   n.csrf,
                   n.irrf,
                   n.percentual_irrf,
                   n.inss,
                   n.iss,
                   n.valor_liquido,
                   n.valor_liquido_correto,
                   n.status_valor_liquido,
                   n.irrf_calculado,
                   n.csrf_calculado,
                   n.iss_calculado,
                   {STATUS_EXPR} as status,
                   {STATUS_FILA_EXPR} as status_fila,
                   {STATUS_FILA_EXPR} as status_exibicao,
                   n.incidencia_iss,
                   n.data_pagamento,
                   n.codigo_servico,
                   n.descricao_servico,
                   n.codigo_nbs,
                   n.codigo_cnae as cnae,
                   n.descricao_cnae,
                   n.simples_xml as simples_nacional,
                   n.consulta_simples_api,
                   n.status_simples_nacional,
                   n.status_csrf,
                   n.status_irrf,
                   n.status_inss,
                   n.status_base_calculo,
                   n.alertas_fiscais,
                   n.observacao_interna,
                   n.status_fila_manual,
                   n.prioridade_manual,
                   n.responsavel,
                   n.created_at as dia_processado,
                   n.updated_at
            FROM nfse_notas n
            LEFT JOIN nfse_processos p ON p.id = %s
            {where}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
            """,
            [processo_id, processo_id, *params, page_size, offset],
        ).fetchall()

        total_row = conn.execute(
            f"SELECT COUNT(*) AS total FROM nfse_notas n {where}",
            params
        ).fetchone()

    return [dict(r) for r in rows], _extract_total(total_row)


def listar_notas_agrupadas(filters: Optional[dict] = None, page: int = 1, page_size: int = 200) -> tuple[List[dict], int]:
    offset = (page - 1) * page_size
    where, params = _build_where(filters)

    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT n.id,
                   COALESCE(ppn.processo_id, n.processo_id) as processo_id,
                   COALESCE(p.cert_alias, n.cert_alias) as certificado,
                   n.tipo_nota,
                   n.numero_documento,
                   n.competencia,
                   n.municipio,
                   n.chave_nfse as chave_acesso,
                   n.data_emissao,
                   COALESCE(n.parte_exibicao_doc, n.cnpj_prestador, '—') as cnpj_cpf,
                   COALESCE(n.parte_exibicao_nome, n.razao_social, '—') as razao_social,
                   COALESCE(n.parte_exibicao_tipo, '') as parte_exibicao_tipo,
                   COALESCE(n.parte_exibicao_nome, n.razao_social, '—') as parte_exibicao_nome,
                   COALESCE(n.parte_exibicao_doc, n.cnpj_prestador, '—') as parte_exibicao_doc,
                   n.valor_total,
                   n.valor_bc as valor_base,
                   n.percentual_irrf,
                   n.incidencia_iss,
                   n.data_pagamento,
                   n.codigo_servico,
                   n.descricao_servico,
                   n.codigo_nbs,
                   n.codigo_cnae as cnae,
                   n.descricao_cnae,
                   n.simples_xml as simples_nacional,
                   n.consulta_simples_api,
                   n.status_simples_nacional,
                   n.status_csrf,
                   n.status_irrf,
                   n.status_inss,
                   n.status_base_calculo,
                   n.valor_liquido,
                   n.valor_liquido_correto,
                   n.status_valor_liquido,
                   n.csrf,
                   n.irrf,
                   n.iss,
                   n.inss,
                   n.irrf_calculado,
                   n.csrf_calculado,
                   n.iss_calculado,
                   {STATUS_EXPR} as status,
                   {STATUS_FILA_EXPR} as status_fila,
                   {STATUS_FILA_EXPR} as status_exibicao,
                   n.alertas_fiscais,
                   n.observacao_interna,
                   n.status_fila_manual,
                   n.prioridade_manual,
                   n.responsavel,
                   n.created_at as dia_processado,
                   n.created_at,
                   n.updated_at
            FROM nfse_notas n
            LEFT JOIN LATERAL (
                SELECT ppn.processo_id
                FROM nfse_processo_notas ppn
                WHERE ppn.nota_id = n.id
                ORDER BY ppn.created_at DESC, ppn.processo_id DESC
                LIMIT 1
            ) ppn ON TRUE
            LEFT JOIN nfse_processos p ON p.id = COALESCE(ppn.processo_id, n.processo_id)
            {where}
            ORDER BY n.updated_at DESC, n.created_at DESC
            LIMIT %s OFFSET %s
            """,
            [*params, page_size, offset],
        ).fetchall()

        total_row = conn.execute(
            f"SELECT COUNT(*) AS total FROM nfse_notas n {where}",
            params
        ).fetchone()

    return [dict(r) for r in rows], _extract_total(total_row)


def obter_resumo_processo(processo_id: str) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) as total_notas,
                COALESCE(SUM(CASE WHEN {STATUS_EXPR} = 'correta' THEN 1 ELSE 0 END), 0) as total_corretas,
                COALESCE(SUM(CASE WHEN {STATUS_EXPR} = 'divergente' THEN 1 ELSE 0 END), 0) as total_divergentes,
                COALESCE(SUM(n.valor_total), 0) as valor_total_processado
            FROM nfse_notas n
            WHERE EXISTS (
                SELECT 1 FROM nfse_processo_notas ppn
                WHERE ppn.nota_id = n.id AND ppn.processo_id = %s
            )
            """,
            (processo_id,),
        ).fetchone()

    resumo = dict(row or {})
    resumo["total_notas"] = resumo.get("total_notas") or 0
    resumo["total_corretas"] = resumo.get("total_corretas") or 0
    resumo["total_divergentes"] = resumo.get("total_divergentes") or 0
    resumo["valor_total_processado"] = resumo.get("valor_total_processado") or 0
    resumo["principais_municipios"] = []
    resumo["principais_codigos_servico"] = []
    resumo["principais_alertas"] = []
    return resumo


def backfill_comparativo_tributos(limit: Optional[int] = None) -> int:
    from .nfse_xml_converter import NFSeXMLConverter

    conv = NFSeXMLConverter(tipo_nota="tomados")
    limit_sql = ""
    params: List[Any] = []
    if limit is not None and limit > 0:
        limit_sql = "LIMIT %s"
        params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, codigo_servico, valor_total, valor_bc, iss, simples_xml
            FROM nfse_notas
            WHERE irrf_calculado IS NULL
               OR csrf_calculado IS NULL
               OR iss_calculado IS NULL
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()

        atualizados = 0
        for row in rows:
            valor_total = float(row["valor_total"] or 0)
            valor_bc = float(row["valor_bc"] or 0)
            base_calculo = valor_bc if valor_bc > 0 else valor_total
            dados = {
                "Valor Total": valor_total,
                "Valor B/C": valor_bc,
                "Simples Nacional / XML": row["simples_xml"] or "",
            }
            comparativo = conv.aplicar_regras_retencao(dados, row["codigo_servico"]) or {}
            categoria = conv._categoria_simples(row["simples_xml"] or "")

            irrf_calculado = comparativo.get("irrf_esperado")
            csrf_calculado = comparativo.get("csrf_esperado")
            iss_calculado = float(row["iss"] or 0)

            if categoria in ("MEI", "OPTANTE") or base_calculo == 0:
                irrf_calculado = 0.0
                csrf_calculado = 0.0

            conn.execute(
                """
                UPDATE nfse_notas
                SET irrf_calculado = %s,
                    csrf_calculado = %s,
                    iss_calculado = %s
                WHERE id = %s
                """,
                (
                    irrf_calculado if irrf_calculado is not None else 0.0,
                    csrf_calculado if csrf_calculado is not None else 0.0,
                    iss_calculado,
                    row["id"],
                ),
            )
            atualizados += 1

    return atualizados


def _normalize_file_key(value: Any) -> str:
    txt = str(value or "").strip().lower()
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(ch for ch in txt if unicodedata.category(ch) != "Mn")
    txt = re.sub(r"[^a-z0-9]+", "", txt)
    return txt


def _digits_only(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _score_arquivo_para_nota(row: dict, tipo_arquivo: str, arquivo_origem_nome: str, arquivo_origem_stem: str, numero_documento: str, chave_nfse: str) -> int:
    nome = str(row.get("nome_arquivo") or "")
    stem = Path(nome).stem
    nome_norm = _normalize_file_key(nome)
    stem_norm = _normalize_file_key(stem)
    digits_nome = _digits_only(nome)

    score = 0
    if arquivo_origem_nome and nome.lower() == arquivo_origem_nome.lower():
        score = max(score, 120 if tipo_arquivo == "xml" else 95)
    if arquivo_origem_stem and stem.lower() == arquivo_origem_stem.lower():
        score = max(score, 115 if tipo_arquivo == "xml" else 100)
    if arquivo_origem_stem and arquivo_origem_stem in stem_norm:
        score = max(score, 90)
    if numero_documento and numero_documento in digits_nome:
        score = max(score, 70)
    if chave_nfse and len(chave_nfse) >= 8 and chave_nfse in digits_nome:
        score = max(score, 80)
    if arquivo_origem_nome and _normalize_file_key(arquivo_origem_nome) in nome_norm:
        score = max(score, 85)
    return score


def localizar_documentos_nota(nota_id: int) -> dict:
    garantir_schema_nfse_notas()
    with get_conn() as conn:
        nota = conn.execute(
            """
            SELECT
              n.id,
              COALESCE(ppn.processo_id, n.processo_id) AS processo_id,
              n.numero_documento,
              n.chave_nfse,
              n.arquivo_origem,
              n.dados_completos
            FROM nfse_notas n
            LEFT JOIN LATERAL (
              SELECT ppn.processo_id
              FROM nfse_processo_notas ppn
              WHERE ppn.nota_id = n.id
              ORDER BY ppn.created_at DESC, ppn.processo_id DESC
              LIMIT 1
            ) ppn ON TRUE
            WHERE n.id = %s
            """,
            (nota_id,),
        ).fetchone()
        if not nota:
            return {"nota_id": nota_id, "processo_id": None, "xml": None, "pdf": None}

        processo_id = str(nota["processo_id"]) if nota["processo_id"] else None
        if not processo_id:
            return {"nota_id": nota_id, "processo_id": None, "xml": None, "pdf": None}

        arquivos = conn.execute(
            """
            SELECT id, processo_id, tipo_arquivo, nome_arquivo, storage_key, caminho_local, content_type, tamanho_bytes, competencia, created_at
            FROM nfse_processo_arquivos
            WHERE processo_id = %s
              AND tipo_arquivo IN ('xml', 'pdf')
            ORDER BY created_at DESC, id DESC
            """,
            (processo_id,),
        ).fetchall()

    dados = nota["dados_completos"] or {}
    arquivo_origem_nome = Path(str(nota["arquivo_origem"] or "")).name
    arquivo_origem_stem = _normalize_file_key(Path(arquivo_origem_nome).stem) if arquivo_origem_nome else ""
    numero_documento = _digits_only(nota["numero_documento"] or dados.get("N° Documento") or dados.get("Nº Documento"))
    chave_nfse = _digits_only(nota["chave_nfse"] or dados.get("Chave de Acesso"))

    best = {"xml": None, "pdf": None}
    best_score = {"xml": -1, "pdf": -1}

    for row in arquivos:
        item = dict(row)
        tipo = str(item.get("tipo_arquivo") or "")
        score = _score_arquivo_para_nota(item, tipo, arquivo_origem_nome, arquivo_origem_stem, numero_documento, chave_nfse)
        if score > best_score.get(tipo, -1):
            best[tipo] = item
            best_score[tipo] = score

    return {
        "nota_id": nota_id,
        "processo_id": processo_id,
        "xml": best["xml"],
        "pdf": best["pdf"],
    }