# modules/downloader.py
"""
Módulo de Download e Organização de Arquivos NFS-e
"""
import os
import time
import shutil
import re
import glob
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
import PyPDF2
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from .login import login_manual
import xml.etree.ElementTree as ET
import traceback
import os
from datetime import datetime

# Variáveis globais
TENTATIVAS_TOTAIS = 3
arquivos_associados = {}

def download_arquivo_paralelo(url, filename, download_dir, session=None):
    """Download de arquivo em paralelo usando sessão persistente"""
    try:
        if session is None:
            session = requests.Session()
        
        response = session.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        filepath = os.path.join(download_dir, filename)
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        return True, filepath
    except Exception as e:
        print(f"  ⚠️ Erro ao baixar {filename}: {e}")
        return False, None

def extrair_info_pdf(caminho_pdf):
    """Extrai número da nota e nome do prestador de um arquivo PDF"""
    try:
        with open(caminho_pdf, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            texto = ""
            
            for pagina in pdf_reader.pages:
                texto += pagina.extract_text()
            
            linhas = texto.split('\n')
            
            numero_nota = None
            nome_prestador = None
            
            # Buscar Número da NFS-e
            for i, linha in enumerate(linhas):
                linha_limpa = linha.strip()
                
                if "Número da NFS-e" in linha_limpa:
                    partes = linha_limpa.split("Número da NFS-e")
                    if len(partes) > 1:
                        numero_nota = partes[1].strip()
                    elif i + 1 < len(linhas):
                        numero_nota = linhas[i + 1].strip()
                    break
            
            # Buscar Nome do Prestador
            for i, linha in enumerate(linhas):
                linha_limpa = linha.strip()
                
                if "Nome / Nome Empresarial" in linha_limpa:
                    partes = linha_limpa.split("Nome / Nome Empresarial")
                    if len(partes) > 1:
                        nome_prestador = partes[1].strip()
                    elif i + 1 < len(linhas):
                        nome_prestador = linhas[i + 1].strip()
                    break
            
            # Se não encontrou pelo formato específico, tentar outros padrões
            if not nome_prestador:
                for i, linha in enumerate(linhas):
                    linha_limpa = linha.strip()
                    if "EMITENTE DA NFS-e" in linha_limpa:
                        for j in range(i + 1, min(i + 10, len(linhas))):
                            if linhas[j].strip() and "Nome" not in linhas[j] and "CNPJ" not in linhas[j]:
                                nome_prestador = linhas[j].strip()
                                break
                        break
            
            # Limpar e validar os valores encontrados
            if numero_nota:
                match = re.search(r'\d+', numero_nota)
                if match:
                    numero_nota = match.group()
                else:
                    numero_nota = "DESCONHECIDO"
            else:
                numero_nota = "DESCONHECIDO"
            
            if nome_prestador:
                nome_prestador = re.sub(r'[^\w\s-]', '', nome_prestador)
                nome_prestador = nome_prestador.replace(' ', '_').replace('/', '_').replace('\\', '_')
                nome_prestador = nome_prestador.strip('_')
                nome_prestador = nome_prestador[:50]
                
                if len(nome_prestador) < 3:
                    nome_prestador = "DESCONHECIDO"
            else:
                nome_prestador = "DESCONHECIDO"
            
            return numero_nota, nome_prestador
            
    except Exception as e:
        print(f"   ⚠️ Erro ao extrair informações do PDF {os.path.basename(caminho_pdf)}: {e}")
        return "DESCONHECIDO", "DESCONHECIDO"

def extrair_info_xml(caminho_xml):
    """Extrai número da nota e nome do prestador de um arquivo XML"""
    try:
        tree = ET.parse(caminho_xml)
        root = tree.getroot()
        
        ns = {'ns': 'http://www.sped.fazenda.gov.br/nfse'}
        
        # Extrair número da nota
        nNFSe_element = root.find('.//ns:nNFSe', ns)
        if nNFSe_element is not None:
            numero_nota = nNFSe_element.text.strip()
        else:
            nNFSe_element = root.find('.//nNFSe')
            if nNFSe_element is not None:
                numero_nota = nNFSe_element.text.strip()
            else:
                numero_nota = "DESCONHECIDO"
        
        # Extrair nome do prestador
        xNome_element = root.find('.//ns:emit/ns:xNome', ns)
        if xNome_element is not None:
            nome_prestador = xNome_element.text.strip()
        else:
            xNome_element = root.find('.//emit/xNome')
            if xNome_element is not None:
                nome_prestador = xNome_element.text.strip()
            else:
                nome_prestador = "DESCONHECIDO"
        
        # Limpar nome do prestador para usar como nome de arquivo
        nome_prestador_limpo = re.sub(r'[^\w\s-]', '', nome_prestador)
        nome_prestador_limpo = nome_prestador_limpo.replace(' ', '_').replace('/', '_').replace('\\', '_')
        nome_prestador_limpo = nome_prestador_limpo.strip('_')
        nome_prestador_limpo = nome_prestador_limpo[:50]
        
        return numero_nota, nome_prestador_limpo
        
    except Exception as e:
        print(f"   ⚠️ Erro ao extrair informações do XML {os.path.basename(caminho_xml)}: {e}")
        return "DESCONHECIDO", "DESCONHECIDO"

def associar_xml_pdf(download_dir):
    """Associa arquivos XML e PDF que foram baixados juntos"""
    global arquivos_associados
    
    try:
        arquivos = os.listdir(download_dir)
        xmls = [f for f in arquivos if f.lower().endswith('.xml')]
        
        for xml_file in xmls:
            xml_base = xml_file.replace('.xml', '').replace('.XML', '')
            
            # Buscar PDFs com nome similar
            pdfs_similares = []
            for pdf_file in [f for f in arquivos if f.lower().endswith('.pdf')]:
                pdf_base = pdf_file.replace('.pdf', '').replace('.PDF', '')
                
                # Verificar se os nomes são similares (mesmo padrão de download)
                if xml_base in pdf_base or pdf_base in xml_base:
                    pdfs_similares.append(pdf_file)
            
            # Se encontrou PDFs similares, associar o primeiro
            if pdfs_similares:
                # Priorizar PDFs que começam com o mesmo nome
                for pdf in pdfs_similares:
                    if pdf.startswith(xml_base):
                        arquivos_associados[xml_file] = pdf
                        print(f"   ✅ Associado: {xml_file} -> {pdf}")
                        break
                else:
                    # Se não encontrar PDF que começa com o mesmo nome, usar o primeiro
                    arquivos_associados[xml_file] = pdfs_similares[0]
                    print(f"   ✅ Associado: {xml_file} -> {pdfs_similares[0]}")
        
        print(f"   ℹ️ Total de associações encontradas: {len(arquivos_associados)}")
        
    except Exception as e:
        print(f"   ⚠️ Erro ao associar XMLs e PDFs: {e}")

def renomear_arquivos_nfse(download_dir):
    """Renomeia arquivos XML e PDF baixados usando associações"""
    global arquivos_associados
    
    arquivos_renomeados = []
    
    try:
        # Primeiro, associar XMLs e PDFs
        associar_xml_pdf(download_dir)
        
        arquivos = os.listdir(download_dir)
        
        # Processar XMLs primeiro
        for arquivo in arquivos:
            if arquivo.lower().endswith('.xml'):
                caminho_xml = os.path.join(download_dir, arquivo)
                
                # Extrair informações do XML
                numero_nota, nome_prestador = extrair_info_xml(caminho_xml)
                
                # Criar novo nome base
                novo_nome_base = f"{nome_prestador} NFS-e {numero_nota}"
                
                # Renomear XML
                novo_nome_xml = f"{novo_nome_base}.xml"
                novo_caminho_xml = os.path.join(download_dir, novo_nome_xml)
                
                try:
                    contador = 1
                    while os.path.exists(novo_caminho_xml):
                        novo_nome_xml = f"{novo_nome_base}_{contador}.xml"
                        novo_caminho_xml = os.path.join(download_dir, novo_nome_xml)
                        contador += 1
                    
                    os.rename(caminho_xml, novo_caminho_xml)
                    arquivos_renomeados.append((arquivo, novo_nome_xml, 'xml'))
                    print(f"   ✅ XML renomeado: {arquivo} -> {novo_nome_xml}")
                    
                    # AGORA RENOMEAR O PDF ASSOCIADO COM O MESMO NOME BASE
                    pdf_associado = arquivos_associados.get(arquivo)
                    
                    if pdf_associado and os.path.exists(os.path.join(download_dir, pdf_associado)):
                        caminho_pdf = os.path.join(download_dir, pdf_associado)
                        
                        # Usar o MESMO nome base do XML para o PDF
                        novo_nome_pdf = f"{novo_nome_base}.pdf"
                        novo_caminho_pdf = os.path.join(download_dir, novo_nome_pdf)
                        
                        # Verificar se já existe arquivo com esse nome
                        contador_pdf = 1
                        while os.path.exists(novo_caminho_pdf):
                            novo_nome_pdf = f"{novo_nome_base}_{contador_pdf}.pdf"
                            novo_caminho_pdf = os.path.join(download_dir, novo_nome_pdf)
                            contador_pdf += 1
                        
                        # Renomear o PDF
                        os.rename(caminho_pdf, novo_caminho_pdf)
                        arquivos_renomeados.append((pdf_associado, novo_nome_pdf, 'pdf'))
                        print(f"   ✅ PDF renomeado (mesmo nome base): {pdf_associado} -> {novo_nome_pdf}")
                        
                except Exception as e:
                    print(f"   ⚠️ Erro ao renomear {arquivo}: {e}")
        
        # Processar PDFs que não foram associados a XMLs (caso algum tenha escapado)
        for arquivo in arquivos:
            if arquivo.lower().endswith('.pdf'):
                caminho_pdf = os.path.join(download_dir, arquivo)
                
                # Verificar se este PDF já foi renomeado
                ja_renomeado = any(original == arquivo for original, _, _ in arquivos_renomeados)
                
                if not ja_renomeado:
                    # Tentar extrair informações do PDF
                    numero_nota, nome_prestador = extrair_info_pdf(caminho_pdf)
                    
                    novo_nome_base = f"{nome_prestador} NFS-e {numero_nota}"
                    novo_nome_pdf = f"{novo_nome_base}.pdf"
                    novo_caminho_pdf = os.path.join(download_dir, novo_nome_pdf)
                    
                    try:
                        contador = 1
                        while os.path.exists(novo_caminho_pdf):
                            novo_nome_pdf = f"{novo_nome_base}_{contador}.pdf"
                            novo_caminho_pdf = os.path.join(download_dir, novo_nome_pdf)
                            contador += 1
                        
                        os.rename(caminho_pdf, novo_caminho_pdf)
                        arquivos_renomeados.append((arquivo, novo_nome_pdf, 'pdf'))
                        print(f"   ✅ PDF renomeado (extraído): {arquivo} -> {novo_nome_pdf}")
                    except Exception as e:
                        print(f"   ⚠️ Erro ao renomear PDF {arquivo}: {e}")
    
    except Exception as e:
        print(f"   ⚠️ Erro geral ao renomear arquivos: {e}")
    
    return arquivos_renomeados

def verificar_erro_carregamento(driver):
    """Verifica se há erros de carregamento na página"""
    try:
        estado_pagina = driver.execute_script("return document.readyState")
        if estado_pagina != "complete":
            print(f"   ⚠️ Página não carregou completamente: {estado_pagina}")
            return True
        
        erros_comuns = [
            "erro", "error", "falha", "timeout", "timed out", "não carregou",
            "carregamento", "loading", "server error", "service unavailable",
            "gateway", "502", "503", "504", "500", "404", "not found"
        ]
        
        titulo = driver.title.lower()
        url = driver.current_url.lower()
        
        for erro in erros_comuns:
            if erro in titulo or erro in url:
                print(f"   ⚠️ Erro detectado no título/URL: {erro}")
                return True
        
        elementos_erro = driver.find_elements(By.XPATH, 
            "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'erro') or " +
            "contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'error') or " +
            "contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'falha') or " +
            "contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'timeout') or " +
            "contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'carregamento')]")
        
        for elemento in elementos_erro:
            if elemento.is_displayed() and len(elemento.text.strip()) > 0:
                print(f"   ⚠️ Elemento de erro encontrado: {elemento.text[:50]}")
                return True
        
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if len(body_text.strip()) < 50:
                print("   ⚠️ Página com pouco conteúdo (possível erro)")
                return True
        except:
            print("   ⚠️ Não foi possível verificar conteúdo da página")
            return True
        
        return False
        
    except Exception as e:
        print(f"   ⚠️ Erro ao verificar carregamento: {e}")
        return True



def criar_estrutura_pastas(base_dir: str, data_processamento: str = "", data_referencia=None):
    """Cria uma estrutura de pastas estável por certificado e competência (YYYY/MM).

    ✅ NOVA ESTRUTURA (obrigatória):
      base_dir/
        YYYY/
          MM/
            xml/
            pdf/
            planilhas/

    Observações:
    - base_dir já deve ser a pasta do certificado (ex.: <BASE>/<alias>).
    - tmp_downloads fica em: base_dir/tmp_downloads/<run_id>/ (criada no runner).
    - data_referencia deve ser datetime/date (preferido). Se não for fornecido, usa o mês atual.
    """
    from datetime import datetime as _dt

    if data_referencia is None:
        data_referencia = _dt.now()
    ano = getattr(data_referencia, 'year', _dt.now().year)
    mes = getattr(data_referencia, 'month', _dt.now().month)

    ano_dir = os.path.join(base_dir, f"{ano:04d}")
    competencia_dir = os.path.join(ano_dir, f"{mes:02d}")
    xml_dir = os.path.join(competencia_dir, "xml")
    pdf_dir = os.path.join(competencia_dir, "pdf")
    planilhas_dir = os.path.join(competencia_dir, "planilhas")

    for p in (xml_dir, pdf_dir, planilhas_dir):
        os.makedirs(p, exist_ok=True)

    return {
        "competencia": f"{ano:04d}-{mes:02d}",
        "competencia_dir": competencia_dir,
        "xml_dir": xml_dir,
        "pdf_dir": pdf_dir,
        "planilhas_dir": planilhas_dir,
    }


def obter_pasta_destino(tipo_arquivo: str, base_dir: str, data_referencia=None) -> str:
    """Retorna pasta de destino (xml/pdf) dentro da competência."""
    estrutura = criar_estrutura_pastas(base_dir, data_referencia=data_referencia)
    if (tipo_arquivo or "").lower() == "xml":
        return estrutura["xml_dir"]
    return estrutura["pdf_dir"]


def inferir_ano_mes_do_xml(caminho_xml: str) -> tuple[int, int] | None:
    """Extrai (ano, mes) EXCLUSIVAMENTE de <dhProc> para organizar arquivos.

    Observações:
    - dhProc vem em ISO 8601 (ex.: 2026-02-01T14:14:47-03:00).
    - XML pode ter namespace; por isso usamos root.iter() e endswith().
    - Não usamos pandas nem fallback para dhEmi/dCompet.
    """
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(caminho_xml)
        root = tree.getroot()

        for el in root.iter():
            if el.tag.endswith('dhProc') and el.text:
                dhproc = el.text.strip()
                # substring direta do ISO 8601
                ano = dhproc[:4]
                mes = dhproc[5:7]
                return int(ano), int(mes)
    except Exception:
        return None
    return None


def distribuir_por_competencia(download_dir: str, base_dir_cert: str) -> dict[str, list[str]]:
    """Move XML/PDF baixados para <YYYY>/<MM>/xml|pdf baseado EXCLUSIVAMENTE em dhProc (processamento).
    
    Fonte de data:
    - dhProc (data/hora de processamento) - ÚNICA fonte
    - Mês atual - último recurso (apenas se dhProc não for encontrado)
    """
    moved = {"xml": [], "pdf": []}
    if not os.path.isdir(download_dir):
        return moved

    # garante associações e renomeia dentro do tmp
    associar_xml_pdf(download_dir)
    renomear_arquivos_nfse(download_dir)

    arquivos = os.listdir(download_dir)
    xmls = [f for f in arquivos if f.lower().endswith('.xml')]

    for xml_name in xmls:
        xml_path = os.path.join(download_dir, xml_name)
        
        # Extrair dhEmi e dCompet para log
        dhProc_encontrado = None
        dhEmi_encontrado = None
        dCompet_encontrado = None
        
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(xml_path)
            root = tree.getroot()
            def _find_text(tag: str):
                for el in root.iter():
                    if el.tag.endswith(tag) and el.text:
                        return el.text.strip()
                return None
            dhProc_encontrado = _find_text('dhProc')
            dhEmi_encontrado = _find_text('dhEmi')
            dCompet_encontrado = _find_text('dCompet')
        except Exception:
            pass
        
        ym = inferir_ano_mes_do_xml(xml_path)
        if ym is None:
            from datetime import datetime as _dt
            now = _dt.now()
            ym = (now.year, now.month)

        y, m = ym
        
        # Log da organização por emissão
        print(f"   📂 Organizando por dhProc: {xml_name}")
        print(f"      dhProc={dhProc_encontrado}, dhEmi={dhEmi_encontrado}, dCompet={dCompet_encontrado} -> {y:04d}/{m:02d}")
        
        estrutura = criar_estrutura_pastas(base_dir_cert, data_referencia=datetime(y, m, 1))
        xml_dest = estrutura['xml_dir']
        pdf_dest = estrutura['pdf_dir']

        moved_xml = _move_sem_duplicar(xml_path, xml_dest)
        if moved_xml:
            moved['xml'].append(moved_xml)

        base = os.path.splitext(xml_name)[0]
        cand_pdf = os.path.join(download_dir, base + '.pdf')
        if os.path.exists(cand_pdf):
            moved_pdf = _move_sem_duplicar(cand_pdf, pdf_dest)
            if moved_pdf:
                moved['pdf'].append(moved_pdf)

    # PDFs soltos (sem XML) -> mês atual
    arquivos = os.listdir(download_dir)
    pdfs = [f for f in arquivos if f.lower().endswith('.pdf')]
    if pdfs:
        from datetime import datetime as _dt
        estrutura = criar_estrutura_pastas(base_dir_cert, data_referencia=_dt.now())
        for pdf_name in pdfs:
            moved_pdf = _move_sem_duplicar(os.path.join(download_dir, pdf_name), estrutura['pdf_dir'])
            if moved_pdf:
                moved['pdf'].append(moved_pdf)

    return moved


def _move_sem_duplicar(src_path: str, dst_dir: str) -> str | None:
    """Move arquivo para dst_dir evitando duplicações.

    - Se já existir com o mesmo tamanho, não move (descarta duplicata) e retorna None.
    - Se existir com tamanho diferente, cria sufixo _dupN.
    """
    if not os.path.exists(src_path) or not os.path.isfile(src_path):
        return None

    base = os.path.basename(src_path)
    dst_path = os.path.join(dst_dir, base)

    try:
        if os.path.exists(dst_path):
            src_size = os.path.getsize(src_path)
            dst_size = os.path.getsize(dst_path)
            if src_size == dst_size:
                # duplicata idêntica
                os.remove(src_path)
                return None

            name, ext = os.path.splitext(base)
            n = 1
            while True:
                cand = os.path.join(dst_dir, f"{name}_dup{n}{ext}")
                if not os.path.exists(cand):
                    dst_path = cand
                    break
                n += 1

        shutil.move(src_path, dst_path)
        return dst_path
    except Exception:
        return None


def organizar_arquivos_baixados(download_dir, data_referencia, diretorio_base):
    """Organiza os arquivos baixados nas pastas corretas.

    IMPORTANTE:
    - Para evitar erros de mês (competência vs emissão), a organização de pastas deve usar
      EXCLUSIVAMENTE o <dhProc> presente dentro de cada XML.
    - Este wrapper mantém compatibilidade com chamadas antigas que passavam data_referencia,
      mas ignora esse parâmetro e delega para distribuir_por_competencia().
    """
    _ = data_referencia  # mantido por compatibilidade
    moved = distribuir_por_competencia(download_dir, diretorio_base)
    # manter retorno antigo (lista de arquivos movidos)
    return moved.get("xml", []) + moved.get("pdf", [])

def organizar_arquivos_baixados_legacy(download_dir, data_referencia, diretorio_base):
    """(LEGACY) Organiza os arquivos baixados nas pastas antigas por data_processamento."""
    
    arquivos_movidos = []
    
    print("   📝 Renomeando arquivos com padrão NFSE-e...")
    arquivos_renomeados = renomear_arquivos_nfse(download_dir)
    
    try:
        arquivos = os.listdir(download_dir)
    except:
        return arquivos_movidos
    
    # Criar a estrutura de pastas no diretório base
    data_processamento = datetime.now().strftime('%d%m%Y')
    criar_estrutura_pastas(diretorio_base, data_processamento)
    
    for arquivo in arquivos:
        caminho_origem = os.path.join(download_dir, arquivo)
        
        if os.path.isfile(caminho_origem):
            if arquivo.lower().endswith('.xml'):
                tipo = 'xml'
            elif arquivo.lower().endswith('.pdf'):
                tipo = 'pdf'
            else:
                continue
            
            pasta_destino = obter_pasta_destino(tipo, diretorio_base, data_processamento)
            
            caminho_destino = os.path.join(pasta_destino, arquivo)
            
            try:
                shutil.move(caminho_origem, caminho_destino)
                arquivos_movidos.append((arquivo, tipo, pasta_destino))
                print(f"   📂 Movido {arquivo} para {pasta_destino}")
            except Exception as e:
                print(f"   ⚠️ Erro ao mover {arquivo}: {e}")
    
    return arquivos_movidos

def executar_fluxo_com_datas(driver, download_dir, data_inicial, data_final, diretorio_base):
    """Executa o fluxo de download com as datas fornecidas"""
    
    print(f"\n📅 EXECUTANDO FLUXO PARA PERÍODO: {data_inicial} a {data_final}")
    
    try:
        print("\n1. Clicando em NFS-e Recebidas...")
        time.sleep(3)
        
        try:
            link_recebidas = driver.find_element(By.XPATH, "//a[@href='/EmissorNacional/Notas/Recebidas']")
            link_recebidas.click()
            print("   ✅ Clicado em NFS-e Recebidas")
            time.sleep(3)
        except Exception as e:
            print(f"   ❌ Erro ao clicar em NFS-e Recebidas: {e}")
            try:
                driver.get("https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas")
                print("   ✅ Navegado diretamente para NFS-e Recebidas")
                time.sleep(3)
            except:
                raise Exception("Não foi possível acessar NFS-e Recebidas")
        
        if verificar_erro_carregamento(driver):
            raise Exception("Erro no carregamento da página de NFS-e Recebidas")
        
        print(f"2. Preenchendo datas: {data_inicial} a {data_final}...")
        time.sleep(2)
        
        try:
            campo_inicial = None
            campo_final = None
            
            try:
                campo_inicial = driver.find_element(By.XPATH, "//input[contains(@placeholder, 'Data Inicial') or contains(@placeholder, 'data inicial')]")
                campo_final = driver.find_element(By.XPATH, "//input[contains(@placeholder, 'Data Final') or contains(@placeholder, 'data final')]")
                print("   ✅ Campos encontrados por placeholder")
            except:
                try:
                    labels = driver.find_elements(By.TAG_NAME, "label")
                    for label in labels:
                        if "Data Inicial" in label.text:
                            input_id = label.get_attribute("for")
                            if input_id:
                                campo_inicial = driver.find_element(By.ID, input_id)
                            else:
                                campo_inicial = label.find_element(By.XPATH, "following::input[1]")
                        elif "Data Final" in label.text:
                            input_id = label.get_attribute("for")
                            if input_id:
                                campo_final = driver.find_element(By.ID, input_id)
                            else:
                                campo_final = label.find_element(By.XPATH, "following::input[1]")
                    print("   ✅ Campos encontrados por labels")
                except:
                    inputs = driver.find_elements(By.TAG_NAME, "input")
                    campos_data = []
                    for inp in inputs:
                        inp_type = inp.get_attribute("type")
                        if inp_type in ["date", "text"]:
                            campos_data.append(inp)
                    
                    if len(campos_data) >= 2:
                        campo_inicial = campos_data[0]
                        campo_final = campos_data[1]
                        print("   ✅ Campos encontrados por tipo")
            
            if campo_inicial and campo_final:
                campo_inicial.clear()
                campo_inicial.send_keys(data_inicial)
                print(f"   ✅ Data inicial preenchida: {data_inicial}")
                
                time.sleep(1)
                
                campo_final.clear()
                campo_final.send_keys(data_final)
                print(f"   ✅ Data final preenchida: {data_final}")
                
                time.sleep(1)
                
                try:
                    botao_filtrar = driver.find_element(By.XPATH, "//button[contains(text(), 'Filtrar') or contains(@value, 'Filtrar')]")
                    botao_filtrar.click()
                    print("   ✅ Botão Filtrar clicado")
                except:
                    try:
                        botao_filtrar = driver.find_element(By.XPATH, "//input[@type='submit' and contains(@value, 'Filtrar')]")
                        botao_filtrar.click()
                        print("   ✅ Botão Filtrar (input) clicado")
                    except:
                        campo_final.send_keys(Keys.ENTER)
                        print("   ✅ Enter pressionado para filtrar")
                
                print("\n3. Aguardando carregamento da tabela...")
                time.sleep(5)
                
                pagina_atual = 1
                total_xmls_baixados = 0
                total_pdfs_baixados = 0
                
                while True:
                    print(f"\n   📄 Processando página {pagina_atual}...")
                    
                    try:
                        mensagens_vazias = [
                            "nenhum registro", "nenhum resultado", "não há dados", "sem resultados",
                            "nenhuma nota", "no records", "empty", "no data"
                        ]
                        
                        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                        
                        for mensagem in mensagens_vazias:
                            if mensagem in page_text:
                                try:
                                    elemento_mensagem = driver.find_element(By.XPATH, 
                                        f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{mensagem}')]")
                                    if elemento_mensagem.is_displayed():
                                        print(f"   ℹ️ Nenhuma nota encontrada: '{mensagem}'")
                                        raise Exception("Nenhum registro encontrado")
                                except:
                                    continue
                        
                        try:
                            linhas_tabela = driver.find_elements(By.XPATH, "//table//tr[td]")
                            if len(linhas_tabela) == 0:
                                print("   ℹ️ Tabela vazia encontrada")
                                break
                        except:
                            pass
                            
                    except Exception as e_vazio:
                        if "Nenhum registro encontrado" in str(e_vazio):
                            break
                    
                    try:
                        elementos_vazios = driver.find_elements(By.XPATH, "//*[contains(text(), 'Nenhum registro') or contains(text(), 'nenhum resultado') or contains(text(), 'Não há dados') or contains(text(), 'sem resultados')]")
                        if elementos_vazios:
                            for elemento in elementos_vazios:
                                if elemento.is_displayed():
                                    print("   ℹ️ Nenhuma nota encontrada para o período selecionado.")
                                    break
                            else:
                                break
                    except:
                        pass
                    
                    try:
                        time.sleep(3)
                        
                        menus_suspensos = driver.find_elements(By.XPATH, "//a[contains(@class, 'icone-trigger')] | //button[.//i[contains(@class, 'glyphicon-option-vertical')]] | //a[.//i[contains(@class, 'glyphicon-option-vertical')]]")
                        
                        if len(menus_suspensos) == 0:
                            print("   ℹ️ Nenhuma nota encontrada para o período selecionado.")
                            break
                            
                        print(f"   ✅ Encontrados {len(menus_suspensos)} botões de menu suspenso na página")
                        
                        for i, menu_trigger in enumerate(menus_suspensos):
                            try:
                                nota_numero = total_xmls_baixados + total_pdfs_baixados + 1
                                print(f"      📋 Nota {nota_numero}: Abrindo menu suspenso...")
                                
                                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", menu_trigger)
                                time.sleep(1)
                                
                                if menu_trigger.is_displayed() and menu_trigger.is_enabled():
                                    menu_trigger.click()
                                    print(f"      ✅ Menu suspenso aberto")
                                    time.sleep(0.5)
                                    
                                    try:
                                        time.sleep(0.5)
                                        
                                        try:
                                            links_xml = driver.find_elements(By.XPATH, "//a[contains(., 'Download XML') or contains(., 'download XML') or contains(., 'XML')]")
                                            
                                            for link in links_xml:
                                                if link.is_displayed():
                                                    link.click()
                                                    total_xmls_baixados += 1
                                                    print(f"         📄 Download XML {total_xmls_baixados} iniciado")
                                                    time.sleep(0.3)
                                                    break
                                        except Exception as e_xml:
                                            print(f"         ⚠️ Erro ao clicar em XML: {str(e_xml)[:50]}")
                                        
                                        try:
                                            try:
                                                if not driver.find_element(By.XPATH, "//a[contains(., 'Download DANFS-e')]").is_displayed():
                                                    menu_trigger.click()
                                                    time.sleep(0.3)
                                            except:
                                                menu_trigger.click()
                                                time.sleep(0.3)
                                            
                                            links_danfse = driver.find_elements(By.XPATH, "//a[contains(., 'Download DANFS-e') or contains(., 'DANFS-e') or contains(., 'DANFSe') or contains(., 'PDF')]")
                                            
                                            for link in links_danfse:
                                                if link.is_displayed():
                                                    link.click()
                                                    total_pdfs_baixados += 1
                                                    print(f"         📄 Download DANFS-e {total_pdfs_baixados} iniciado")
                                                    time.sleep(0.5)
                                                    break
                                        except Exception as e_pdf:
                                            print(f"         ⚠️ Erro ao clicar em DANFS-e: {str(e_pdf)[:50]}")
                                            
                                    except Exception as e_download:
                                        print(f"         ⚠️ Erro ao tentar baixar arquivos: {str(e_download)[:100]}")
                                    
                                    try:
                                        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
                                        time.sleep(0.5)
                                    except:
                                        pass
                                    
                                else:
                                    print(f"      ⚠️ Menu trigger não está clicável")
                                
                            except Exception as e:
                                print(f"      ⚠️ Erro ao processar nota: {str(e)[:100]}")
                                try:
                                    driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
                                    time.sleep(0.5)
                                except:
                                    pass
                                continue
                        
                    except Exception as e:
                        print(f"   ⚠️ Erro ao encontrar menus suspensos: {e}")
                    
                    print(f"\n   📊 Resumo página {pagina_atual}:")
                    print(f"      XMLs baixados: {total_xmls_baixados}")
                    print(f"      PDFs baixados: {total_pdfs_baixados}")
                    
                    try:
                        print(f"\n   🔍 Verificando próxima página...")
                        
                        encontrou_proxima = False
                        link_proxima = None
                        
                        try:
                            icones_proxima = driver.find_elements(By.XPATH, 
                                "//i[contains(@class, 'fa-chevron-right') or contains(@class, 'fa-angle-right') or contains(@class, 'glyphicon-chevron-right')] | " +
                                "//span[contains(@class, 'next')] | " +
                                "//a[contains(@class, 'next')]")
                            
                            for icone in icones_proxima:
                                try:
                                    elemento_pai = icone.find_element(By.XPATH, "./ancestor::a | ./ancestor::button")
                                    if elemento_pai and elemento_pai.is_displayed():
                                        link_proxima = elemento_pai
                                        print(f"      ✅ Encontrado por ícone: {icone.get_attribute('class')}")
                                        break
                                except:
                                    continue
                        except:
                            pass
                        
                        if not link_proxima:
                            try:
                                textos_proxima = driver.find_elements(By.XPATH,
                                    "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'próxima')] | " +
                                    "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'próximo')] | " +
                                    "//a[text()='>'] | //a[text()='»'] | " +
                                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTuvwxyz', 'abcdefghijklmnopqrstuvwxyz'), 'próxima')]")
                                
                                for elemento in textos_proxima:
                                    if elemento.is_displayed():
                                        link_proxima = elemento
                                        print(f"      ✅ Encontrado por texto: {elemento.text}")
                                        break
                            except:
                                pass
                        
                        if not link_proxima:
                            try:
                                elementos_paginacao = driver.find_elements(By.XPATH,
                                    "//a[contains(@class, 'page-link')] | " +
                                    "//li[contains(@class, 'pagination')]//a | " +
                                    "//div[contains(@class, 'pagination')]//a")
                                
                                for elemento in elementos_paginacao:
                                    texto = elemento.text.strip().lower()
                                    if texto in ['próxima', 'próximo', '>', '»', 'next', '>'] and elemento.is_displayed():
                                        link_proxima = elemento
                                        print(f"      ✅ Encontrado por classe de paginação: {elemento.text}")
                                        break
                            except:
                                pass
                        
                        if link_proxima:
                            is_disabled = False
                            
                            classe = link_proxima.get_attribute("class") or ""
                            estilo = link_proxima.get_attribute("style") or ""
                            disabled_attr = link_proxima.get_attribute("disabled")
                            aria_disabled = link_proxima.get_attribute("aria-disabled")
                            onclick = link_proxima.get_attribute("onclick") or ""
                            
                            disabled_conditions = [
                                "disabled" in classe.lower(),
                                "inactive" in classe.lower(),
                                "invisible" in classe.lower(),
                                "hidden" in classe.lower(),
                                disabled_attr is not None and disabled_attr != "false",
                                aria_disabled and aria_disabled.lower() == "true",
                                "display: none" in estilo.lower(),
                                "visibility: hidden" in estilo.lower(),
                                "opacity: 0" in estilo.lower(),
                                "pointer-events: none" in estilo.lower(),
                                not link_proxima.is_enabled(),
                                not link_proxima.is_displayed(),
                                link_proxima.find_elements(By.XPATH, "./ancestor::li[contains(@class, 'disabled')]"),
                                "return false" in onclick or (not onclick and link_proxima.tag_name == "a" and link_proxima.get_attribute("href") == "#"),
                            ]
                            
                            try:
                                location = link_proxima.location
                                size = link_proxima.size
                                if size['width'] == 0 or size['height'] == 0:
                                    disabled_conditions.append(True)
                                    print(f"      Elemento com tamanho zero: {size}")
                            except:
                                pass
                            
                            try:
                                elemento_pai = link_proxima.find_element(By.XPATH, "./..")
                                classe_pai = elemento_pai.get_attribute("class") or ""
                                if "disabled" in classe_pai.lower():
                                    disabled_conditions.append(True)
                                    print(f"      Elemento pai está desabilitado")
                            except:
                                pass
                            
                            try:
                                texto_paginacao = driver.find_element(By.XPATH, "//div[contains(@class, 'pagination-info') or contains(@class, 'paging')]").text
                                if "1 de 1" in texto_paginacao or "page 1 of 1" in texto_paginacao.lower():
                                    disabled_conditions.append(True)
                                    print(f"      Texto de paginação indica última página: {texto_paginacao}")
                            except:
                                pass
                            
                            if any(disabled_conditions):
                                is_disabled = True
                                print(f"      ⛔ Botão de próxima página DESABILITADO")
                                print(f"      Razões detectadas:")
                                for i, condition in enumerate(disabled_conditions[:10]):
                                    if condition:
                                        print(f"        - Condição {i+1} ativada")
                            else:
                                print(f"      ✅ Botão de próxima página HABILITADO")
                            
                            if not is_disabled:
                                try:
                                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link_proxima)
                                    time.sleep(1)
                                    
                                    try:
                                        link_proxima.click()
                                        print(f"      ↪️ Indo para página {pagina_atual + 1}")
                                    except:
                                        driver.execute_script("arguments[0].click();", link_proxima)
                                        print(f"      ↪️ Indo para página {pagina_atual + 1} (via JavaScript)")
                                    
                                    pagina_atual += 1
                                    time.sleep(5)
                                    continue
                                except Exception as e_click:
                                    print(f"      ⚠️ Erro ao clicar na próxima página: {str(e_click)[:100]}")
                                    print(f"      ✅ Provavelmente última página alcançada")
                                    break
                            else:
                                print(f"      ✅ Última página alcançada (botão desabilitado)")
                                break
                        
                        else:
                            print(f"      ✅ Não há botão de próxima página (última página ou paginação única)")
                            
                            try:
                                indicadores_pagina = driver.find_elements(By.XPATH, 
                                    "//span[contains(@class, 'page-info') or contains(@class, 'pagination')]")
                                
                                for indicador in indicadores_pagina:
                                    texto = indicador.text
                                    if "1" in texto and ("1" == texto.strip() or "1 de 1" in texto or "page 1 of 1" in texto.lower()):
                                        print(f"      Confirmado: página 1 de 1")
                                        break
                            except:
                                pass
                            
                            break

                    except Exception as e_proxima:
                        print(f"      ✅ Não há mais páginas para processar: {str(e_proxima)[:100]}")
                        break
                
                print(f"\n4. 🗂️ Organizando e renomeando arquivos baixados...")
                
                # Usar o diretório base fornecido pelo usuário
                data_referencia = datetime.strptime(data_inicial, '%d/%m/%Y')
                mes = data_referencia.strftime('%m')
                data_processamento = datetime.now().strftime('%d%m%Y')
                criar_estrutura_pastas(diretorio_base, data_processamento)
                
                print("   Aguardando 5 segundos para finalizar downloads...")
                time.sleep(5)
                
                arquivos_organizados = organizar_arquivos_baixados(download_dir, data_referencia, diretorio_base)
                
                print(f"\n{'='*60}")
                print("✅ PROCESSO CONCLUÍDO! (DOWNLOAD)")
                print(f"{'='*60}")
                print(f"📅 Período processado: {data_inicial} a {data_final}")
                print(f"📄 Total de XMLs baixados: {total_xmls_baixados}")
                print(f"📄 Total de PDFs baixados: {total_pdfs_baixados}")
                
                xml_dirs = sorted(glob.glob(os.path.join(diretorio_base, 'xml_processado_*')))
                xml_folder = xml_dirs[-1] if xml_dirs else os.path.join(diretorio_base, f'xml_processado_{data_processamento}')
                pdf_dirs = sorted(glob.glob(os.path.join(diretorio_base, 'pdf_processado_*')))
                pdf_folder = pdf_dirs[-1] if pdf_dirs else os.path.join(diretorio_base, f'pdf_processado_{data_processamento}')
                
                print(f"   XMLs: {xml_folder}")
                print(f"   PDFs: {pdf_folder}")
                
                if arquivos_organizados:
                    xml_count = sum(1 for _, tipo, _ in arquivos_organizados if tipo == 'xml')
                    pdf_count = sum(1 for _, tipo, _ in arquivos_organizados if tipo == 'pdf')
                    print(f"\n📋 Arquivos organizados:")
                    print(f"   XMLs: {xml_count} arquivos")
                    print(f"   PDFs: {pdf_count} arquivos")
                    
                    print(f"\n📝 Exemplos de arquivos renomeados (padrão NOME_PRESTADOR NFS-e NUMERO):")
                    exemplos = []
                    for arquivo, tipo, pasta in arquivos_organizados:
                        if "NFS-e" in arquivo:
                            exemplos.append(arquivo)
                        if len(exemplos) >= 5:
                            break
                    
                    for exemplo in exemplos:
                        print(f"   {exemplo}")
                else:
                    print(f"\n📋 Nenhum arquivo foi organizado (possível erro no download)")
                
                print(f"\n⏱️ A página permanecerá aberta por 10 segundos...")
                time.sleep(10)
                
                return True, download_dir, total_xmls_baixados
                
            else:
                print("   ❌ Não foi possível encontrar os campos de data")
                return False, None, 0
                
        except Exception as e:
            print(f"   ❌ Erro ao preencher datas: {e}")
            traceback.print_exc()
            return False, None, 0
        
    except Exception as e:
        print(f"❌ ERRO durante a execução: {e}")
        traceback.print_exc()
        return False, None, 0

def executar_fluxo_nfse(driver, download_dir, data_inicial, data_final, diretorio_base):
    """
    Executa o fluxo simplificado da NFS-e com sistema de reinicialização.
    Agora recebe o driver já logado e o diretório base.
    """
    
    print(f"\n{'='*60}")
    print(f"INICIANDO DOWNLOAD NFS-e")
    print(f"Período: {data_inicial} a {data_final}")
    print(f"Diretório base: {diretorio_base}")
    print(f"{'='*60}\n")
    
    global arquivos_associados
    arquivos_associados = {}
    
    tentativa = 1
    sucesso = False
    total_xmls_baixados = 0
    
    while tentativa <= TENTATIVAS_TOTAIS and not sucesso:
        print(f"\n{'='*60}")
        print(f"TENTATIVA {tentativa}/{TENTATIVAS_TOTAIS}")
        print(f"{'='*60}")
        
        try:
            sucesso_temp, download_dir_temp, xmls_temp = executar_fluxo_com_datas(
                driver, download_dir, data_inicial, data_final, diretorio_base
            )
            
            if sucesso_temp:
                sucesso = True
                total_xmls_baixados = xmls_temp
                print(f"✅ Tentativa {tentativa} bem-sucedida!")
                break
            else:
                print(f"❌ Tentativa {tentativa} falhou.")
                
                if tentativa < TENTATIVAS_TOTAIS:
                    print(f"🔄 Reiniciando navegador para tentativa {tentativa + 1}...")
                    driver.quit()
                    time.sleep(2)
                    
                    # Re-fazer login manual
                    driver, download_dir = login_manual()
                    if driver is None:
                        print("❌ Não foi possível fazer login novamente.")
                        break
                else:
                    print("❌ Todas as tentativas falharam.")
                    
        except Exception as e:
            print(f"❌ Erro na tentativa {tentativa}: {e}")
            traceback.print_exc()
            
            if tentativa < TENTATIVAS_TOTAIS:
                print(f"🔄 Reiniciando navegador para tentativa {tentativa + 1}...")
                try:
                    driver.quit()
                except:
                    pass
                time.sleep(2)
                
                # Re-fazer login manual
                driver, download_dir = login_manual()
                if driver is None:
                    print("❌ Não foi possível fazer login novamente.")
                    break
            else:
                print("❌ Todas as tentativas falharam.")
        
        tentativa += 1
    
    return sucesso, total_xmls_baixados