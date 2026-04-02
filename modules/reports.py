from typing import Dict, Any, List
from datetime import date
import os
import json
import pandas as pd
from pathlib import Path

from .notas_repo import listar_notas_por_processo, obter_resumo_processo
from .processos_repo import obter_processo
from .db import get_conn


def gerar_relatorio_processo(processo_id: str) -> Dict[str, Any]:
    """
    Gera estrutura de relatório por processo para OBJ3.
    """
    # Get processo info
    processo = obter_processo(processo_id)
    if not processo:
        raise ValueError(f"Processo {processo_id} não encontrado")
    
    cert_alias = processo.cert_alias
    tipo_nota = processo.tipo_nota
    
    # Competencia: use start_date formatted YYYY-MM
    competencia = processo.start_date.strftime('%Y-%m')
    
    # Resumo
    resumo = obter_resumo_processo(processo_id)
    
    # Itens: listar_notas_por_processo (already normalized!)
    filters = {}  # full list
    itens, _ = listar_notas_por_processo(processo_id, filters=filters, page=1, page_size=10000)
    
    # Clean itens for frontend (keep only key fields)
    itens_clean = []
    for item in itens:
        itens_clean.append({
            "numero_documento": item.get("numero_documento"),
            "data_emissao": item.get("data_emissao"),
            "parte_exibicao_nome": item.get("parte_exibicao_nome"),
            "parte_exibicao_doc": item.get("parte_exibicao_doc"),
            "valor_total": float(item.get("valor_total") or 0),
            "status": item.get("status"),
            "observacao": item.get("alertas_fiscais") or "",
            "correcao_manual": "",  # future
            "competencia": item.get("competencia"),
            "codigo_servico": item.get("codigo_servico")
        })
    
    return {
        "processo_id": processo_id,
        "certificado": cert_alias,
        "competencia": competencia,
        "tipo_nota": tipo_nota,
        "resumo": resumo,
        "itens": itens_clean
    }


def save_report_files(relatorio_data: Dict[str, Any], base_dir: str = "resultados"):
    """
    Save relatorio as JSON/CSV/XLSX in resultados/CERT/AAAA-MM/
    """
    cert = relatorio_data["certificado"]
    competencia = relatorio_data["competencia"]
    
    dir_path = Path(base_dir) / cert / competencia
    dir_path.mkdir(parents=True, exist_ok=True)
    
    # JSON
    json_path = dir_path / "relatorio.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(relatorio_data, f, ensure_ascii=False, indent=2)
    
    # CSV + XLSX from itens
    itens_df = pd.DataFrame(relatorio_data["itens"])
    
    csv_path = dir_path / "relatorio.csv"
    itens_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    
    xlsx_path = dir_path / "relatorio.xlsx"
    itens_df.to_excel(xlsx_path, index=False, sheet_name="Itens")
    
    print(f"📁 Reports saved: {dir_path}")
    return {
        "json": str(json_path),
        "csv": str(csv_path),
        "xlsx": str(xlsx_path)
    }

