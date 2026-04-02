import json
from datetime import datetime, timedelta
from .db import get_conn
from psycopg.types.json import Json

def init_cache_database():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cnpj_cache (
          cnpj              CHAR(14) PRIMARY KEY,
          data_consulta     DATE NOT NULL,
          fonte             TEXT NOT NULL,
          status            TEXT NOT NULL,
          simples_status    TEXT,
          codigo_cnae       TEXT,
          descricao_cnae    TEXT,
          json_completo     JSONB NOT NULL,
          data_expiracao    DATE NOT NULL,
          updated_at        TIMESTAMP NOT NULL DEFAULT now()
        );
        """)
        # upgrades
        conn.execute("ALTER TABLE cnpj_cache ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT now()")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cnpj_cache_expiracao ON cnpj_cache (data_expiracao);")
    return True

def buscar_cnpj_no_cache(cnpj_limpo: str, fonte_preferida: str = "OpenCaramelo"):
    """
    Busca no cache apenas registros não expirados e da fonte preferida.
    Isso evita reutilizar dados antigos (ex.: OpenCaramelo/outros provedores) após troca de provedor.
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT cnpj, data_consulta, fonte, status, simples_status, codigo_cnae, descricao_cnae, json_completo, data_expiracao
            FROM cnpj_cache
            WHERE cnpj = %s
              AND data_expiracao >= CURRENT_DATE
              AND fonte = %s
        """, (cnpj_limpo, fonte_preferida)).fetchone()

        if not row:
            return None

        dados = dict(row)
        if isinstance(dados["json_completo"], str):
            dados["json_completo"] = json.loads(dados["json_completo"])
        return dados

def salvar_cnpj_no_cache(cnpj_limpo: str, dados_api: dict, fonte="OpenCaramelo"):
    status = dados_api.get("status", "ERRO")

    simples_status = None
    if status == "OK" and isinstance(dados_api.get("simples"), dict) and "optante" in dados_api["simples"]:
        simples_status = "Optante S.N" if dados_api["simples"]["optante"] else "Não optante"

    codigo_cnae = None
    descricao_cnae = None
    atividades = dados_api.get("atividade_principal")
    if isinstance(atividades, list) and atividades and isinstance(atividades[0], dict):
        codigo_cnae = atividades[0].get("code", "")
        descricao_cnae = atividades[0].get("text", "")

    data_consulta = datetime.now().date()
    data_expiracao = (datetime.now() + timedelta(days=30)).date()

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO cnpj_cache
            (cnpj, data_consulta, fonte, status, simples_status, codigo_cnae, descricao_cnae, json_completo, data_expiracao, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (cnpj) DO UPDATE SET
              data_consulta = EXCLUDED.data_consulta,
              fonte = EXCLUDED.fonte,
              status = EXCLUDED.status,
              simples_status = EXCLUDED.simples_status,
              codigo_cnae = EXCLUDED.codigo_cnae,
              descricao_cnae = EXCLUDED.descricao_cnae,
              json_completo = EXCLUDED.json_completo,
              data_expiracao = EXCLUDED.data_expiracao,
              updated_at = now()
        """, (cnpj_limpo, data_consulta, fonte, status, simples_status, codigo_cnae, descricao_cnae, Json(dados_api), data_expiracao))
    return True

def limpar_cache_expirado():
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM cnpj_cache WHERE data_expiracao < CURRENT_DATE")
        return cur.rowcount

def obter_estatisticas_cache():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM cnpj_cache").fetchone()["total"]
        validos = conn.execute(
            "SELECT COUNT(*) AS validos FROM cnpj_cache WHERE data_expiracao >= CURRENT_DATE"
        ).fetchone()["validos"]

    return {"total": total, "validos": validos, "expirados": total - validos}
