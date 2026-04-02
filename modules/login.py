# modules/login.py
"""
Módulo de Login Manual no Portal NFS-e
"""
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import time
import os

def login_manual():
    """
    Abre o navegador no portal NFS-e para login manual.
    Retorna o driver quando detecta que o usuário está logado.
    """
    
    print(f"\n{'='*60}")
    print("LOGIN MANUAL NO PORTAL NFS-e")
    print("="*60)
    print("\n1. O navegador será aberto no portal NFS-e.")
    print("2. Faça o login manualmente usando seu certificado digital.")
    print("3. Após o login, o sistema detectará automaticamente e continuará.")
    print("\n" + "="*60)
    
    # Configurar navegador
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    
    # Configurar diretório de download temporário (será ajustado depois)
    download_dir = os.path.join(os.getcwd(), "downloads_nfse_temp")
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)
    
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "plugins.always_open_pdf_externally": True,
        "profile.default_content_settings.popups": 0
    }
    chrome_options.add_experimental_option("prefs", prefs)
    
    driver = None
    
    try:
        print("\nAbrindo navegador...")
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(30)
        driver.maximize_window()
        time.sleep(2)
        
        print("Acessando o portal NFS-e...")
        driver.get("https://www.nfse.gov.br/EmissorNacional/Login?ReturnUrl=%2fEmissorNacional%2f")
        
        print("\n" + "="*60)
        print("AGUARDANDO LOGIN MANUAL...")
        print("="*60)
        print("\nPor favor, faça o login manualmente.")
        print("O sistema aguardará até que o login seja detectado.")
        print("\n" + "="*60)
        
        # Aguardar até que o elemento de NFS-e Recebidas esteja presente
        wait = WebDriverWait(driver, 300)  # 5 minutos de timeout
        elemento_recebidas = wait.until(
            EC.presence_of_element_located((By.XPATH, "//a[@href='/EmissorNacional/Notas/Recebidas']"))
        )
        
        print("✅ Login detectado com sucesso!")
        
        return driver, download_dir
        
    except Exception as e:
        print(f"❌ Erro durante o login manual: {e}")
        if driver:
            driver.quit()
        return None, None