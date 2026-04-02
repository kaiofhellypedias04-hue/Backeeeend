 # main.py
"""
Sistema de Auditoria Fiscal NFS-e
Versão 2.0 - Com Auditoria Fiscal e Cache Persistente
"""
import traceback
import glob
import os
import time
from pathlib import Path
from datetime import date, datetime, timedelta
import json
from dotenv import load_dotenv

from modules.gui import obter_configuracoes_iniciais
from modules.nfse_xml_converter import NFSeXMLConverterComAPI
from modules.cache import init_cache_database, limpar_cache_expirado
from modules.runner import RunConfig, run_processing
from modules.settings import get_settings
from modules.spreadsheet import atualizar_planilha_incremental

load_dotenv()


def carregar_certificados(caminho: str):
    if not os.path.exists(caminho):
        print(f"❌ Arquivo de certificados não encontrado: {caminho}")
        print("   Configure os certificados na interface (Gerenciar Certificados) ou crie um 'certs.json'.")
        return []
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            print("❌ certs.json inválido: esperado um array com certificados.")
            return []
        return data
    except Exception as e:
        print(f"❌ Erro ao ler certs.json: {e}")
        return []


def carregar_credenciais(caminho: str):
    """Carrega credenciais CPF/CNPJ do arquivo JSON."""
    if not os.path.exists(caminho):
        return []
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            return []
        return data
    except Exception as e:
        print(f"❌ Erro ao ler credentials.json: {e}")
        return []


def _parse_br_date(s: str):
    return datetime.strptime(s, "%d/%m/%Y").date()




def _sleep_until_next(hhmm: str):
    """Bloqueia até o próximo horário HH:MM (horário local)."""
    alvo = datetime.strptime(hhmm, "%H:%M").time()
    while True:
        now = datetime.now()
        target_dt = datetime.combine(now.date(), alvo)
        
        # Se o alvo já passou HOJE, agenda para AMANHÃ
        if target_dt <= now:
            target_dt = target_dt + timedelta(days=1)
        
        delta = (target_dt - now).total_seconds()
        
        # Se delta <= 1 segundo, considera que é hora de executar
        if delta <= 1:
            print(f"[DEBUG] Hora de executar! Agora: {now.strftime('%H:%M:%S')}")
            return
        
        print(f"[DEBUG] Agora: {now.strftime('%H:%M:%S')} | Proxima: {target_dt.strftime('%d/%m %H:%M:%S')} | Delta: {int(delta)}s")
        
        # Dormir em passos, mas acordar 1 segundo antes do horário
        sleep_time = min(delta - 1, 30)
        if sleep_time > 0:
            time.sleep(sleep_time)


def run_automatico_diario(rcfg: RunConfig, hhmm: str, config: dict):
    """Executa o processamento automático todos os dias no horário hhmm (HH:MM).
    OBS: precisa manter o programa/PC ligado.
    """
    print(f"\n⏰ Agendado: execução diária às {hhmm} (mantenha o programa aberto).\n")
    while True:
        _sleep_until_next(hhmm)
        try:
            print(f"\n▶️ Execução automática iniciando em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}...")
            
            # Verifica se o usuário definiu uma data inicial personalizada
            data_inicial_config = config.get('data_inicial')
            
            hoje = date.today()
            data_fim = hoje
            
            if data_inicial_config:
                # Usa a data inicial definida pelo usuário
                try:
                    data_inicio = _parse_br_date(data_inicial_config)
                    print(f"📅 Período automático: {data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')} (definido pelo usuário)")
                except Exception:
                    # Se falhar, volta para os últimos 30 dias
                    data_inicio = hoje - timedelta(days=29)
                    print(f"📅 Período automático: {data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')} (últimos 30 dias)")
            else:
                # Padrão: sempre processar os ÚLTIMOS 30 DIAS
                data_inicio = hoje - timedelta(days=29)
                print(f"📅 Período automático: {data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')} (últimos 30 dias)")
            
            # Atualiza a configuração com as novas datas
            rcfg.start = data_inicio
            rcfg.end = data_fim
            
            run_processing(rcfg)
        except KeyboardInterrupt:
            # ✅ REQUISITO 3: Tratar KeyboardInterrupt para encerrar limpo
            print("\n\n⏹️ Execução interrompida pelo usuário (Ctrl+C). Encerrando...")
            break
        except Exception as e:
            print(f"❌ Erro na execução automática: {e}")
            traceback.print_exc()
    
    print("💤 Modo automático encerrado.")

def main():
    """Função principal que orquestra todo o processo"""

    print(f"\n{'='*60}")
    print("SISTEMA DE AUDITORIA FISCAL NFS-e")
    print("="*60)
    print("Versão 2.0 - Com Auditoria Fiscal e Cache Persistente")
    print("="*60)

    # Inicializa cache (se aplicável)
    try:
        init_cache_database()
        limpar_cache_expirado()
    except Exception:
        pass

    # Obter configurações do usuário
    config = obter_configuracoes_iniciais()

    if not config['modo']:
        print("Operação cancelada pelo usuário.")
        return

    if config['modo'] in ('download', 'single_cert', 'automatic'):
        modo_exibicao = {
            'download': 'DOWNLOAD E PROCESSAMENTO',
            'single_cert': 'PROCESSAR 1 CERTIFICADO',
            'automatic': 'MODO AUTOMÁTICO DIÁRIO'
        }
        tipo_login_exibicao = {
            'certificado': 'Certificado Digital (PFX)',
            'cpf_cnpj': 'CPF/CNPJ e Senha'
        }
        
        print(f"\n{'='*60}")
        print(f"MODO: {modo_exibicao.get(config['modo'], config['modo'])}")
        print(f"TIPO DE LOGIN: {tipo_login_exibicao.get(config.get('tipo_login', 'certificado'), 'Certificado')}")

        if config['modo'] == 'automatic':
            print("Período: Automático (dia seguinte ao último processado até ontem)")
        else:
            print(f"Período: {config['data_inicial']} a {config['data_final']}")

        if config['modo'] == 'single_cert':
            print(f"Certificado: {config['certificado']}")

        print(f"Diretório base: {config['diretorio_base']}")
        print("="*60)

        settings = get_settings()
        certs_path = str(Path(settings.certs_json_path))
        credentials_path = str(Path(settings.credentials_json_path))
        
        # Obtém o tipo de login da configuração
        login_type = config.get('tipo_login', 'certificado')
        
        if login_type == 'cpf_cnpj':
            # Login por CPF/CNPJ - carregar credenciais
            credenciais = carregar_credenciais(credentials_path)
            if not credenciais:
                print(f"❌ Nenhuma credencial encontrada em: {credentials_path}")
                print("   Configure as credenciais na interface (Gerenciar Credenciais).")
                return
            # Define aliases das credenciais
            aliases = [c.get("alias") for c in credenciais if c.get("alias")]
        else:
            # Login por certificado - carregar certificados
            certificados = carregar_certificados(certs_path)
            if not certificados:
                print(f"❌ Nenhum certificado encontrado em: {certs_path}")
                print("   Configure os certificados na interface (Gerenciar Certificados).")
                return
            # Define aliases dos certificados
            if config['modo'] == 'single_cert':
                aliases = [config['certificado']]
            else:
                aliases = [c.get("alias") for c in certificados if c.get("alias")]

        # Define dates based on mode
        if config['modo'] == 'automatic':
            start = None  # Will be calculated automatically
            end = None
        else:
            start = _parse_br_date(config['data_inicial'])
            end = _parse_br_date(config['data_final'])

        rcfg = RunConfig(
            modo='manual' if config['modo'] != 'automatic' else 'automatico',
            base_dir=config['diretorio_base'],
            certs_json_path=certs_path,
            credentials_json_path=credentials_path,
            cert_aliases=aliases,
            start=start,
            end=end,
            headless=False,
            chunk_days=30,
            consultar_api=True,
            login_type=login_type,
            tipo_nota=config.get('tipo_nota', 'tomados'),
        )
        run_automatico_diario(rcfg, config.get('hora_automatico') or '06:00', config) if config['modo']=='automatic' and config.get('hora_automatico') else run_processing(rcfg)



    else:
        print(f"\n{'='*60}")
        print("MODO: PLANILHAR XMLs JÁ BAIXADOS")
        print(f"Pasta XMLs: {config['pasta_xmls']}")
        print(f"Pasta saída: {config['pasta_saida']}")
        print("="*60)

        xml_files = glob.glob(os.path.join(config['pasta_xmls'], "*.xml"))
        if not xml_files:
            print(f"❌ Nenhum arquivo XML encontrado em: {config['pasta_xmls']}")
            return

        print(f"📄 Encontrados {len(xml_files)} arquivos XML para processar")

        # Obtém o tipo de nota da configuração (padrão: tomados)
        tipo_nota = config.get('tipo_nota', 'tomados')
        converter = NFSeXMLConverterComAPI(tipo_nota=tipo_nota, consultar_api=True)

        print("🔍 Extraindo dados dos XMLs...")
        dados = converter.process_multiple_files(xml_files)

        if not dados:
            print("❌ Nenhum dado foi extraído dos XMLs.")
            return

        print(f"✅ Dados extraídos de {len(dados)} notas fiscais")

        print("\n🔍 Realizando auditoria fiscal (Simples Nacional e CNAE)...")
        dados = converter.consultar_cnpjs_em_lote(dados)

        # Salvar UMA planilha por execução, nomeada pelo período (evita gerar 2+ planilhas por mês da nota)
        # Regra: usar o período inferido a partir dos XMLs (min/max Data de Emissão; fallback: Competência).
        # Se nenhuma data for encontrada, usa a data de hoje.
        def _parse_iso_date(s):
            try:
                if isinstance(s, str) and len(s) >= 10 and "-" in s:
                    return datetime.fromisoformat(s[:10]).date()
            except Exception:
                return None
            return None

        datas = []
        for d in dados:
            dt = _parse_iso_date(d.get("Data de Emissão")) or _parse_iso_date(d.get("Competência"))
            if dt:
                datas.append(dt)

        if datas:
            periodo_start = min(datas)
            periodo_end = max(datas)
        else:
            today = date.today()
            periodo_start = today
            periodo_end = today

        # Pasta de saída: usa a pasta configurada diretamente, SEM criar subpastas por ano/mês
        saida_dir = config['pasta_saida']
        os.makedirs(saida_dir, exist_ok=True)

        # Sempre cria uma NOVA planilha com o novo formato (não usa planilhas existentes)
        # Isso garante que o formato das colunas (CSRF, etc) esteja correto
        today = date.today()
        nome_arquivo = f"auditoria_nfse_{today.isoformat()}.xlsx"
        caminho_planilha = os.path.join(saida_dir, nome_arquivo)

        print(f"💾 Salvando/atualizando planilha: {caminho_planilha}")
        existentes, adicionados = atualizar_planilha_incremental(converter, caminho_planilha, dados)
        print(f"📊 Planilha atualizada: {existentes} já existiam, {adicionados} novos adicionados → {os.path.basename(caminho_planilha)}")

        planilhas_geradas = [caminho_planilha]
        if planilhas_geradas:
            print(f"📊 RESUMO FINAL:")
            print(f"   • Notas processadas: {len(dados)}")
            print(f"   • Planilhas geradas/atualizadas: {len(planilhas_geradas)}")
            for p in planilhas_geradas:
                print(f"     - {p}")
            # tenta abrir a última planilha no Windows
            try:
                os.startfile(planilhas_geradas[-1])
            except Exception:
                pass
        else:
            print("❌ Nenhuma planilha foi gerada (sem datas válidas ou sem dados).")

    print(f"\n{'='*60}")
    print("PROCESSO FINALIZADO!")
    print("="*60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n❌ Programa interrompido pelo usuário.")
    except Exception as e:
        print(f"\n❌ Erro fatal: {e}")
        traceback.print_exc()

    input("\nPressione Enter para sair...")
