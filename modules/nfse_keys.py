import hashlib


def normalizar_valor(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    # remove formatação de moeda comum
    s = s.replace("R$", "").replace(" ", "")
    s = s.replace(".", "").replace(",", ".")
    return s


def gerar_chave_nfse(dados: dict) -> str:
    """Gera uma chave estável por nota.

    Preferência (quando disponível):
      cnpj_prestador + numero_documento + data_emissao + valor_total

    Fallback: hash de campos principais.
    """
    chave_acesso = (dados.get('Chave de Acesso') or dados.get('chave_acesso') or '').strip()
    if chave_acesso:
        return chave_acesso

    cnpj = (dados.get("CNPJ/CPF") or "").strip()
    numero = (dados.get("N° Documento") or dados.get("Numero") or "").strip()
    data_emissao = (dados.get("Data de Emissão") or "").strip()
    valor_total = normalizar_valor(dados.get("Valor Total"))

    base = f"{cnpj}|{numero}|{data_emissao}|{valor_total}"
    if cnpj and numero and data_emissao and valor_total:
        return base

    # fallback seguro
    payload = "|".join([
        cnpj,
        numero,
        data_emissao,
        valor_total,
        str(dados.get("Município") or ""),
        str(dados.get("Razão Social") or ""),
        str(dados.get("Competência") or ""),
    ])
    return "hash:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
