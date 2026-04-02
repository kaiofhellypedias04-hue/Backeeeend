# modules/cnpj_consultor.py
"""modules/cnpj_consultor.py

Consulta de CNPJ usando Invertexto.

Endpoint:
  GET https://api.invertexto.com/v1/cnpj/{cnpj}
  Autenticação:
    - Query param: ?token=SEU_TOKEN
    - ou header: Authorization: Bearer SEU_TOKEN

Observações:
- A Invertexto retorna dados cadastrais e CNAE (principal e secundários). Dependendo do plano,
  pode retornar também informações de Simples/MEI.
- Mantém o contrato "normalizado" que o restante do app já espera:
    - status
    - cnpj
    - atividade_principal: [{code, text}]
    - simples: {optante}
    - simei: {optante}
    - mei (alias de simei)
    - ultima_atualizacao

Configuração:
- Defina o token por variável de ambiente: INVERTEXTO_TOKEN
- (Opcional) Ajuste o throttle por INVERTEXTO_DELAY_SECONDS
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional
import time
import requests
from dotenv import load_dotenv

load_dotenv()

from .cache import (
    buscar_cnpj_no_cache,
    salvar_cnpj_no_cache,
    limpar_cache_expirado,
    obter_estatisticas_cache,
)
RPM = int(os.getenv("INVERTEXTO_RPM", "30"))
MIN_INTERVAL = 60.0 / RPM  # 2.0s quando RPM=30
_next_allowed = 0.0

def throttle():
    global _next_allowed
    now = time.monotonic()
    wait = _next_allowed - now
    if wait > 0:
        time.sleep(wait)
    # agenda a próxima “janela”
    _next_allowed = time.monotonic() + MIN_INTERVAL
    
BASE_URL = "https://api.invertexto.com/v1"
FONTE_CACHE = "Invertexto"

# Throttle padrão (somente quando não veio do cache). Ajuste via INVERTEXTO_DELAY_SECONDS.
WAIT_SECONDS_AFTER_CALL = 0.6


class CNPJConsultor:
    """
    Retorna um JSON "normalizado" para o restante do sistema.

    Campos principais:
      - status: "OK" | "CNPJ_INVALIDO" | "ERRO_API" | ...
      - cnpj: string 14 dígitos
      - atividade_principal: lista com 1 item [{code, text}] (derivado do primeiro CNAE retornado)
      - simples: {"optante": bool|None}
      - simei:  {"optante": bool|None}
      - mei: alias para simei
      - ultima_atualizacao: ISO string
    """

    def __init__(self, delay_seconds: float | None = None):
        # Se delay_seconds for fornecido, respeita. Caso contrário, usa o padrão do rate limit.
        env_delay = os.getenv("INVERTEXTO_DELAY_SECONDS")
        default_delay = float(env_delay) if env_delay else WAIT_SECONDS_AFTER_CALL
        self.delay_seconds = float(delay_seconds) if delay_seconds is not None else default_delay

        self.token = os.getenv("INVERTEXTO_TOKEN")
        if not self.token:
            # Não interrompe o app (para permitir modo sem API), mas deixa claro o motivo.
            print("⚠️  INVERTEXTO_TOKEN não definido. As consultas de CNPJ irão falhar (401/403).")

        self.cache: Dict[str, Dict[str, Any]] = {}
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "test_planilha/invertexto",
        })

        from .cache import init_cache_database
        init_cache_database()
        limpar_cache_expirado()

        stats = obter_estatisticas_cache()
        if stats:
            print(f"📊 Cache: {stats['validos']} válidos, {stats['expirados']} expirados (total {stats['total']})")

    def limpar_cnpj(self, cnpj: Any) -> Optional[str]:
        if not cnpj:
            return None
        return re.sub(r"[^0-9]", "", str(cnpj))

    def consultar_cnpj(self, cnpj: Any, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Consulta com cache:
          1) RAM
          2) Postgres (fonte preferida Invertexto)
          3) Invertexto (1 chamada) + espera (throttle)
        """
        cnpj_limpo = self.limpar_cnpj(cnpj)

        if not cnpj_limpo or len(cnpj_limpo) != 14:
            return {"status": "CNPJ_INVALIDO", "message": "CNPJ inválido"}

        if not force_refresh:
            # 1) RAM
            if cnpj_limpo in self.cache:
                return self.cache[cnpj_limpo]

            # 2) Postgres
            dados_cache = buscar_cnpj_no_cache(cnpj_limpo, fonte_preferida=FONTE_CACHE)
            if dados_cache:
                self.cache[cnpj_limpo] = dados_cache["json_completo"]
                return dados_cache["json_completo"]

        # 3) API
        inicio = time.time()
        raw = self._consultar_api_invertexto(cnpj_limpo)

        normalized = self._normalizar_resposta(cnpj_limpo, raw)

        # cacheia somente sucesso
        if normalized.get("status") == "OK":
            self.cache[cnpj_limpo] = normalized
            salvar_cnpj_no_cache(cnpj_limpo, normalized, fonte=FONTE_CACHE)
        else:
            self.cache[cnpj_limpo] = normalized

        # respeita limite (somente quando houve chamada real)
        elapsed = time.time() - inicio
        wait = max(0.0, self.delay_seconds - elapsed)
        if wait > 0:
            time.sleep(wait)

        return normalized

    def _consultar_api_invertexto(self, cnpj_limpo: str) -> Dict[str, Any]:
        url = f"{BASE_URL}/cnpj/{cnpj_limpo}"

        for tentativa in range(1, 4):
            try:
                headers = {}
                params = {}

                # Invertexto aceita token via query param e/ou Bearer.
                # Preferimos Bearer para evitar token em logs de URL, mas mantemos fallback.
                if self.token:
                    headers["Authorization"] = f"Bearer {self.token}"
                    params["token"] = self.token

                # Aplica throttle antes da chamada à API
                throttle()

                resp = self.session.get(url, headers=headers, params=params, timeout=25)

                if resp.status_code == 429:
                    if tentativa < 3:
                        time.sleep(1.0 * tentativa)
                        continue
                    return {"_status": "RATE_LIMIT", "_http_code": 429, "_message": "Rate limit (429)"}

                if resp.status_code >= 500:
                    if tentativa < 3:
                        time.sleep(1.0 * tentativa)
                        continue
                    return {"_status": "ERRO_SERVIDOR", "_http_code": resp.status_code, "_message": "Erro 5xx"}

                if resp.status_code != 200:
                    snippet = (resp.text or "")[:200]
                    return {"_status": "ERRO_HTTP", "_http_code": resp.status_code, "_message": f"HTTP {resp.status_code}: {snippet}"}

                try:
                    data = resp.json()
                except ValueError:
                    snippet = (resp.text or "")[:200]
                    return {"_status": "ERRO_PARSE", "_message": f"Resposta não-JSON: {snippet}"}

                return data

            except requests.exceptions.Timeout:
                if tentativa < 3:
                    time.sleep(1.0 * tentativa)
                    continue
                return {"_status": "TIMEOUT", "_message": "Timeout na consulta Invertexto"}
            except requests.exceptions.ConnectionError:
                if tentativa < 3:
                    time.sleep(1.0 * tentativa)
                    continue
                return {"_status": "ERRO_CONEXAO", "_message": "Erro de conexão na consulta Invertexto"}
            except Exception as e:
                return {"_status": "ERRO_GERAL", "_message": str(e)}

        return {"_status": "ERRO_GERAL", "_message": "Falha desconhecida"}

    @staticmethod
    def _sn_to_bool(v: Any) -> Optional[bool]:
        if v is None:
            return None
        s = str(v).strip().upper()
        if s == "S":
            return True
        if s == "N":
            return False
        return None

    def _normalizar_resposta(self, cnpj_limpo: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Converte a resposta da Invertexto para o formato que o app já usa."""
        # Se veio um erro internal nosso (_status), devolve payload consistente
        if isinstance(raw, dict) and raw.get("_status"):
            return {
                "status": raw.get("_status", "ERRO_API"),
                "message": raw.get("_message", "Falha ao consultar Invertexto"),
                "cnpj": cnpj_limpo,
                "atividade_principal": [],
                "simples": {"optante": None},
                "simei": {"optante": None},
                "mei": {"optante": None},
                "ultima_atualizacao": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }

        # Invertexto tende a retornar o payload direto (sem "success": true).
        if not isinstance(raw, dict):
            return {
                "status": "ERRO_API",
                "message": (raw.get("message") if isinstance(raw, dict) else None) or "Resposta inesperada da API",
                "cnpj": cnpj_limpo,
                "atividade_principal": [],
                "simples": {"optante": None},
                "simei": {"optante": None},
                "mei": {"optante": None},
                "ultima_atualizacao": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }

        data = raw

        # CNAE: Invertexto costuma usar "atividade_principal" (objeto) e
        # "atividades_secundarias" (lista). Mantemos também aliases antigos.
        cnaes = []
        cnae_principal = (
            data.get("atividade_principal")
            or data.get("cnae_principal")
            or data.get("cnaePrincipal")
        )
        if isinstance(cnae_principal, dict):
            cnaes.append(cnae_principal)

        cnae_sec = (
            data.get("atividades_secundarias")
            or data.get("cnaes_secundarios")
            or data.get("cnae_secundarias")
            or data.get("cnaeSecundarias")
        )
        if isinstance(cnae_sec, list):
            cnaes.extend([c for c in cnae_sec if isinstance(c, dict)])

        atividade_principal = []
        if isinstance(cnaes, list) and cnaes:
            c0 = cnaes[0] or {}
            atividade_principal = [{
                "code": str(c0.get("codigo") or c0.get("cnae") or "").strip(),
                "text": str(c0.get("descricao") or c0.get("text") or "").strip(),
            }]

        # Simples/MEI: dependendo do plano, pode vir em objetos ou flags.
        simples_opt = None
        mei_opt = None

        # 1) Estruturas comuns
        # Invertexto (doc oficial) retorna:
        #   "simples": {"optante_simples": "S|N", ...}
        #   "mei":     {"optante_mei": "S|N", ...}
        # mas alguns planos/versões podem retornar "optante" boolean.
        simples_obj = data.get("simples_nacional") or data.get("simples")
        if isinstance(simples_obj, dict):
            simples_opt = (
                simples_obj.get("optante")
                if "optante" in simples_obj
                else self._sn_to_bool(simples_obj.get("optante_simples"))
            )
        elif simples_obj is not None:
            simples_opt = simples_obj

        mei_obj = data.get("mei") or data.get("simei")
        if isinstance(mei_obj, dict):
            mei_opt = (
                mei_obj.get("optante")
                if "optante" in mei_obj
                else self._sn_to_bool(mei_obj.get("optante_mei"))
            )
        elif mei_obj is not None:
            mei_opt = mei_obj

        # 2) Flags S/N
        if simples_opt is None:
            simples_opt = self._sn_to_bool(data.get("opcao_simples") or data.get("opcaoSimples"))
        if mei_opt is None:
            mei_opt = self._sn_to_bool(data.get("opcao_mei") or data.get("opcaoMei") or data.get("optante_mei"))

        # Normaliza bool/None
        if isinstance(simples_opt, str):
            simples_opt = self._sn_to_bool(simples_opt)
        if isinstance(mei_opt, str):
            mei_opt = self._sn_to_bool(mei_opt)

        if isinstance(simples_opt, int):
            simples_opt = bool(simples_opt)
        if isinstance(mei_opt, int):
            mei_opt = bool(mei_opt)

        normalized: Dict[str, Any] = {
            "status": "OK",
            "cnpj": data.get("cnpj") or data.get("ni") or cnpj_limpo,
            "atividade_principal": atividade_principal,
            "simples": {"optante": simples_opt},
            "simei": {"optante": mei_opt},
            "mei": {"optante": mei_opt},
            "ultima_atualizacao": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

        # Campos extra (mantém compatibilidade com o restante do app)
        mapping = {
            "nome": "razao_social",
            "fantasia": "nome_fantasia",
            # Invertexto retorna "situacao" como objeto {nome,data,motivo};
            # mantemos também compatibilidade com versões que retornem string.
            "situacao": "situacao_cadastral",
            "natureza_juridica": "natureza_juridica",
            "logradouro": "logradouro",
            "numero": "numero",
            "complemento": "complemento",
            "bairro": "bairro",
            "municipio": "municipio",
            "uf": "uf",
            "cep": "cep",
            "telefone": "telefone",
            "email": "email",
            "capital_social": "capital_social",
        }
        for dest, src in mapping.items():
            val = data.get(src)
            if val not in (None, ""):
                normalized[dest] = val

        # Normalizações específicas Invertexto (objetos aninhados)
        situacao_obj = data.get("situacao")
        if isinstance(situacao_obj, dict):
            nome_situacao = situacao_obj.get("nome")
            if nome_situacao:
                normalized["situacao"] = nome_situacao

        end_obj = data.get("endereco")
        if isinstance(end_obj, dict):
            # Só preenche se não existirem ainda
            normalized.setdefault("logradouro", end_obj.get("logradouro"))
            normalized.setdefault("numero", end_obj.get("numero"))
            normalized.setdefault("complemento", end_obj.get("complemento"))
            normalized.setdefault("bairro", end_obj.get("bairro"))
            normalized.setdefault("cep", end_obj.get("cep"))
            normalized.setdefault("uf", end_obj.get("uf"))
            normalized.setdefault("municipio", end_obj.get("municipio"))

        # Telefones Invertexto: telefone1/telefone2
        if "telefone" not in normalized:
            t1 = data.get("telefone1")
            t2 = data.get("telefone2")
            tel = " / ".join([t for t in [t1, t2] if t])
            if tel:
                normalized["telefone"] = tel

        # opcional: mantém listas completas (não quebra nada)
        normalized["cnaes"] = cnaes
        normalized["socios"] = data.get("socios") or data.get("qsa") or []

        return normalized

    def get_simples_status(self, dados_cnpj: Dict[str, Any]) -> str:
        """Extrai status do Simples/MEI a partir do payload normalizado."""
        if not dados_cnpj or dados_cnpj.get("status") != "OK":
            return "Erro na consulta"

        simei = dados_cnpj.get("simei") or {}
        if simei.get("optante") is True:
            return "MEI"

        simples = dados_cnpj.get("simples") or {}
        if simples.get("optante") is True:
            return "Optante S.N"
        if simples.get("optante") is False:
            return "Não optante"

        return "Não disponível"

    def get_cnae_info(self, dados_cnpj: Dict[str, Any]):
        """Extrai CNAE principal no formato {code,text}."""
        if not dados_cnpj or dados_cnpj.get("status") != "OK":
            return None, None

        atividades = dados_cnpj.get("atividade_principal")
        if isinstance(atividades, list) and atividades:
            a0 = atividades[0] or {}
            return a0.get("code"), a0.get("text")

        return None, None
