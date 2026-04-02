# modules/gui.py
"""
Módulo de Interface Gráfica para configurações iniciais
"""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from tkcalendar import DateEntry
from datetime import datetime
import os
from modules.cert_manager import (
    load_certs, upsert_cert, remove_cert, get_password, set_password, delete_password,
    load_credentials, upsert_credential, remove_credential, get_credential_password, 
    set_credential_password, delete_credential_password
)
from modules.settings import get_settings

def obter_configuracoes_iniciais():
    """Cria interface gráfica para o usuário escolher o modo de operação"""
    
    resultado = {
        'modo': None,
        'data_inicial': None,
        'data_final': None,
        'diretorio_base': None,
        'pasta_xmls': None,
        'pasta_saida': None,
        'certificado': None,
        'hora_automatico': None,
        'tipo_login': 'certificado',  # 'certificado' ou 'cpf_cnpj'
        'credencial': None,  # alias da credencial CPF/CNPJ
        'tipo_nota': 'tomados'  # 'tomados' (Recebidas) ou 'prestados' (Emitidas)
    }
    
    def selecionar_diretorio_base():
        nonlocal resultado
        diretorio = filedialog.askdirectory(title="Selecione o diretório base para salvar os arquivos")
        if diretorio:
            resultado['diretorio_base'] = diretorio
            label_diretorio_base.config(text=diretorio)
    
    def selecionar_pasta_xmls():
        nonlocal resultado
        pasta = filedialog.askdirectory(title="Selecione a pasta com os XMLs já baixados")
        if pasta:
            resultado['pasta_xmls'] = pasta
            label_pasta_xmls.config(text=pasta)
    
    def selecionar_pasta_saida():
        nonlocal resultado
        pasta = filedialog.askdirectory(title="Selecione a pasta para salvar a planilha")
        if pasta:
            resultado['pasta_saida'] = pasta
            label_pasta_saida.config(text=pasta)
    

    def gerenciar_certificados():
        """
        Janela para cadastrar/visualizar certificados (.pfx) e salvar senha no cofre do Windows (keyring).
        - Caminhos ficam em certs.json (root do projeto)
        - Senhas ficam no Windows Credential Manager (service: nfse_auditoria)
        """
        try:
            certs_path = str(get_settings().certs_json_path)

            win = tk.Toplevel(root)
            win.title("Gerenciar Certificados (PFX)")
            win.geometry("720x420")
            win.configure(bg="#f0f0f0")
            win.transient(root)
            win.grab_set()

            frame_list = tk.LabelFrame(
                win, text="Certificados configurados",
                font=("Arial", 10, "bold"), bg="#f0f0f0",
                padx=10, pady=10
            )
            frame_list.pack(fill="both", expand=True, padx=15, pady=10)

            listbox = tk.Listbox(frame_list, height=10)
            listbox.pack(side="left", fill="both", expand=True)

            scrollbar = tk.Scrollbar(frame_list, orient="vertical", command=listbox.yview)
            scrollbar.pack(side="right", fill="y")
            listbox.config(yscrollcommand=scrollbar.set)

            info_var = tk.StringVar(value="Selecione um certificado para ver detalhes.")
            info_label = tk.Label(win, textvariable=info_var, bg="#f0f0f0", fg="#333", anchor="w", justify="left")
            info_label.pack(fill="x", padx=15)

            def refresh_list(select_alias=None):
                listbox.delete(0, tk.END)
                certs = load_certs(certs_path)
                for c in certs:
                    alias = c.get("alias", "")
                    pfx = c.get("pfxPath", "")
                    display = f"{alias}  —  {os.path.basename(pfx)}"
                    listbox.insert(tk.END, display)

                if select_alias:
                    for i in range(listbox.size()):
                        if listbox.get(i).startswith(select_alias + " "):
                            listbox.selection_clear(0, tk.END)
                            listbox.selection_set(i)
                            listbox.activate(i)
                            show_selected()
                            break

            def selected_alias():
                sel = listbox.curselection()
                if not sel:
                    return None
                line = listbox.get(sel[0])
                return line.split("—")[0].strip()

            def show_selected(event=None):
                alias = selected_alias()
                if not alias:
                    info_var.set("Selecione um certificado para ver detalhes.")
                    return

                certs = load_certs(certs_path)
                cert = next((c for c in certs if c.get("alias") == alias), None)
                if not cert:
                    info_var.set("Certificado não encontrado.")
                    return

                try:
                    has_pwd = bool(get_password(alias))
                except Exception as e:
                    info_var.set(f"Alias: {alias}\nCaminho: {cert.get('pfxPath')}\n⚠️ Erro ao acessar cofre: {e}")
                    return

                info_var.set(
                    f"Alias: {alias}\n"
                    f"Caminho: {cert.get('pfxPath')}\n"
                    f"Senha no cofre: {'✅ configurada' if has_pwd else '❌ não configurada'}"
                )

            listbox.bind("<<ListboxSelect>>", show_selected)

            frame_buttons = tk.Frame(win, bg="#f0f0f0")
            frame_buttons.pack(fill="x", padx=15, pady=10)

            def adicionar_ou_reconfigurar():
                alias = simpledialog.askstring("Alias", "Digite um nome (alias) para este certificado:", parent=win)
                if not alias:
                    return
                alias = alias.strip()

                pfx_path = filedialog.askopenfilename(
                    parent=win,
                    title="Selecione o certificado .PFX",
                    filetypes=[("Certificado PFX", "*.pfx"), ("Todos os arquivos", "*.*")]
                )
                if not pfx_path:
                    return

                senha = simpledialog.askstring(
                    "Senha do certificado",
                    f"Digite a senha do certificado '{alias}':",
                    parent=win,
                    show="*"
                )
                if senha is None or senha == "":
                    messagebox.showerror("Erro", "Senha não informada.")
                    return

                upsert_cert(certs_path, alias, pfx_path)
                try:
                    set_password(alias, senha)  # sobrescreve se já existir
                except Exception as e:
                    messagebox.showerror(
                        "Erro",
                        "Não foi possível salvar a senha no cofre do Windows.\n\n"
                        f"Detalhes: {e}\n\n"
                        "Dica: instale a biblioteca com: pip install keyring"
                    )
                    return

                messagebox.showinfo("OK", f"Certificado '{alias}' configurado com sucesso.")
                refresh_list(select_alias=alias)

            def atualizar_senha():
                alias = selected_alias()
                if not alias:
                    messagebox.showwarning("Atenção", "Selecione um certificado na lista.")
                    return
                senha = simpledialog.askstring(
                    "Atualizar senha",
                    f"Digite a NOVA senha do certificado '{alias}':",
                    parent=win,
                    show="*"
                )
                if senha is None or senha == "":
                    return
                try:
                    set_password(alias, senha)
                except Exception as e:
                    messagebox.showerror("Erro", f"Falha ao atualizar senha no cofre: {e}")
                    return
                messagebox.showinfo("OK", f"Senha atualizada para '{alias}'.")
                refresh_list(select_alias=alias)

            def atualizar_caminho():
                alias = selected_alias()
                if not alias:
                    messagebox.showwarning("Atenção", "Selecione um certificado na lista.")
                    return
                pfx_path = filedialog.askopenfilename(
                    parent=win,
                    title=f"Selecione o NOVO arquivo .PFX para '{alias}'",
                    filetypes=[("Certificado PFX", "*.pfx"), ("Todos os arquivos", "*.*")]
                )
                if not pfx_path:
                    return
                upsert_cert(certs_path, alias, pfx_path)
                messagebox.showinfo("OK", f"Caminho atualizado para '{alias}'.")
                refresh_list(select_alias=alias)

            def remover_certificado():
                alias = selected_alias()
                if not alias:
                    messagebox.showwarning("Atenção", "Selecione um certificado na lista.")
                    return
                if not messagebox.askyesno(
                    "Confirmar",
                    f"Remover o certificado '{alias}'?\n\n"
                    "Isso remove o caminho do certs.json e também a senha do cofre."
                ):
                    return
                remove_cert(certs_path, alias)
                try:
                    delete_password(alias)
                except Exception:
                    pass
                refresh_list()
                info_var.set("Selecione um certificado para ver detalhes.")

            tk.Button(
                frame_buttons, text="Adicionar / Reconfigurar",
                command=adicionar_ou_reconfigurar,
                bg="#4CAF50", fg="white",
                font=("Arial", 10, "bold"), width=20
            ).grid(row=0, column=0, padx=5, pady=5)

            tk.Button(
                frame_buttons, text="Atualizar caminho",
                command=atualizar_caminho,
                bg="#2196F3", fg="white",
                font=("Arial", 10, "bold"), width=15
            ).grid(row=0, column=1, padx=5, pady=5)

            tk.Button(
                frame_buttons, text="Atualizar senha",
                command=atualizar_senha,
                bg="#FF9800", fg="white",
                font=("Arial", 10, "bold"), width=15
            ).grid(row=0, column=2, padx=5, pady=5)

            tk.Button(
                frame_buttons, text="Remover",
                command=remover_certificado,
                bg="#f44336", fg="white",
                font=("Arial", 10, "bold"), width=10
            ).grid(row=0, column=3, padx=5, pady=5)

            tk.Button(
                frame_buttons, text="Fechar",
                command=win.destroy,
                bg="#777", fg="white",
                font=("Arial", 10, "bold"), width=10
            ).grid(row=0, column=4, padx=5, pady=5)

            refresh_list()

        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao abrir gerenciador de certificados: {e}")


    def gerenciar_credenciais():
        """
        Janela para cadastrar/visualizar credenciais CPF/CNPJ e salvar senha no cofre do Windows.
        - CPF/CNPJ ficam em credentials.json (root do projeto)
        - Senhas ficam no Windows Credential Manager (service: nfse_auditoria_credentials)
        """
        try:
            credentials_path = str(get_settings().credentials_json_path)

            win = tk.Toplevel(root)
            win.title("Gerenciar Credenciais CPF/CNPJ")
            win.geometry("720x420")
            win.configure(bg="#f0f0f0")
            win.transient(root)
            win.grab_set()

            frame_list = tk.LabelFrame(
                win, text="Credenciais configuradas",
                font=("Arial", 10, "bold"), bg="#f0f0f0",
                padx=10, pady=10
            )
            frame_list.pack(fill="both", expand=True, padx=15, pady=10)

            listbox = tk.Listbox(frame_list, height=10)
            listbox.pack(side="left", fill="both", expand=True)

            scrollbar = tk.Scrollbar(frame_list, orient="vertical", command=listbox.yview)
            scrollbar.pack(side="right", fill="y")
            listbox.config(yscrollcommand=scrollbar.set)

            info_var = tk.StringVar(value="Selecione uma credencial para ver detalhes.")
            info_label = tk.Label(win, textvariable=info_var, bg="#f0f0f0", fg="#333", anchor="w", justify="left")
            info_label.pack(fill="x", padx=15)

            def refresh_list(select_alias=None):
                listbox.delete(0, tk.END)
                creds = load_credentials(credentials_path)
                for c in creds:
                    alias = c.get("alias", "")
                    cpf_cnpj = c.get("cpf_cnpj", "")
                    display = f"{alias}  —  {cpf_cnpj}"
                    listbox.insert(tk.END, display)

                if select_alias:
                    for i in range(listbox.size()):
                        if listbox.get(i).startswith(select_alias + " "):
                            listbox.selection_clear(0, tk.END)
                            listbox.selection_set(i)
                            listbox.activate(i)
                            show_selected()
                            break

            def selected_alias():
                sel = listbox.curselection()
                if not sel:
                    return None
                line = listbox.get(sel[0])
                return line.split("—")[0].strip()

            def show_selected(event=None):
                alias = selected_alias()
                if not alias:
                    info_var.set("Selecione uma credencial para ver detalhes.")
                    return

                creds = load_credentials(credentials_path)
                cred = next((c for c in creds if c.get("alias") == alias), None)
                if not cred:
                    info_var.set("Credencial não encontrada.")
                    return

                try:
                    has_pwd = bool(get_credential_password(alias))
                except Exception as e:
                    info_var.set(f"Alias: {alias}\nCPF/CNPJ: {cred.get('cpf_cnpj')}\n⚠️ Erro ao acessar cofre: {e}")
                    return

                info_var.set(
                    f"Alias: {alias}\n"
                    f"CPF/CNPJ: {cred.get('cpf_cnpj')}\n"
                    f"Senha no cofre: {'✅ configurada' if has_pwd else '❌ não configurada'}"
                )

            listbox.bind("<<ListboxSelect>>", show_selected)

            frame_buttons = tk.Frame(win, bg="#f0f0f0")
            frame_buttons.pack(fill="x", padx=15, pady=10)

            def adicionar_ou_reconfigurar():
                alias = simpledialog.askstring("Alias", "Digite um nome (alias) para esta credencial:", parent=win)
                if not alias:
                    return
                alias = alias.strip()

                cpf_cnpj = simpledialog.askstring(
                    "CPF/CNPJ",
                    f"Digite o CPF ou CNPJ para '{alias}':",
                    parent=win
                )
                if not cpf_cnpj or cpf_cnpj.strip() == "":
                    messagebox.showerror("Erro", "CPF/CNPJ não informado.")
                    return
                cpf_cnpj = cpf_cnpj.strip()

                senha = simpledialog.askstring(
                    "Senha do Portal",
                    f"Digite a SENHA do portal NFS-e para '{alias}':",
                    parent=win,
                    show="*"
                )
                if senha is None or senha == "":
                    messagebox.showerror("Erro", "Senha não informada.")
                    return

                upsert_credential(credentials_path, alias, cpf_cnpj)
                try:
                    set_credential_password(alias, senha)  # sobrescreve se já existir
                except Exception as e:
                    messagebox.showerror(
                        "Erro",
                        "Não foi possível salvar a senha no cofre do Windows.\n\n"
                        f"Detalhes: {e}\n\n"
                        "Dica: instale a biblioteca com: pip install keyring"
                    )
                    return

                messagebox.showinfo("OK", f"Credencial '{alias}' configurada com sucesso.")
                refresh_list(select_alias=alias)

            def atualizar_senha():
                alias = selected_alias()
                if not alias:
                    messagebox.showwarning("Atenção", "Selecione uma credencial na lista.")
                    return
                senha = simpledialog.askstring(
                    "Atualizar senha",
                    f"Digite a NOVA senha do portal NFS-e para '{alias}':",
                    parent=win,
                    show="*"
                )
                if senha is None or senha == "":
                    return
                try:
                    set_credential_password(alias, senha)
                except Exception as e:
                    messagebox.showerror("Erro", f"Falha ao atualizar senha no cofre: {e}")
                    return
                messagebox.showinfo("OK", f"Senha atualizada para '{alias}'.")
                refresh_list(select_alias=alias)

            def atualizar_cpf_cnpj():
                alias = selected_alias()
                if not alias:
                    messagebox.showwarning("Atenção", "Selecione uma credencial na lista.")
                    return
                cpf_cnpj = simpledialog.askstring(
                    "Atualizar CPF/CNPJ",
                    f"Digite o NOVO CPF/CNPJ para '{alias}':",
                    parent=win
                )
                if not cpf_cnpj or cpf_cnpj.strip() == "":
                    messagebox.showerror("Erro", "CPF/CNPJ não informado.")
                    return
                upsert_credential(credentials_path, alias, cpf_cnpj.strip())
                messagebox.showinfo("OK", f"CPF/CNPJ atualizado para '{alias}'.")
                refresh_list(select_alias=alias)

            def remover_credencial():
                alias = selected_alias()
                if not alias:
                    messagebox.showwarning("Atenção", "Selecione uma credencial na lista.")
                    return
                if not messagebox.askyesno(
                    "Confirmar",
                    f"Remover a credencial '{alias}'?\n\n"
                    "Isso remove do credentials.json e também a senha do cofre."
                ):
                    return
                remove_credential(credentials_path, alias)
                try:
                    delete_credential_password(alias)
                except Exception:
                    pass
                refresh_list()
                info_var.set("Selecione uma credencial para ver detalhes.")

            tk.Button(
                frame_buttons, text="Adicionar / Reconfigurar",
                command=adicionar_ou_reconfigurar,
                bg="#4CAF50", fg="white",
                font=("Arial", 10, "bold"), width=20
            ).grid(row=0, column=0, padx=5, pady=5)

            tk.Button(
                frame_buttons, text="Atualizar CPF/CNPJ",
                command=atualizar_cpf_cnpj,
                bg="#2196F3", fg="white",
                font=("Arial", 10, "bold"), width=18
            ).grid(row=0, column=1, padx=5, pady=5)

            tk.Button(
                frame_buttons, text="Atualizar senha",
                command=atualizar_senha,
                bg="#FF9800", fg="white",
                font=("Arial", 10, "bold"), width=15
            ).grid(row=0, column=2, padx=5, pady=5)

            tk.Button(
                frame_buttons, text="Remover",
                command=remover_credencial,
                bg="#f44336", fg="white",
                font=("Arial", 10, "bold"), width=10
            ).grid(row=0, column=3, padx=5, pady=5)

            tk.Button(
                frame_buttons, text="Fechar",
                command=win.destroy,
                bg="#777", fg="white",
                font=("Arial", 10, "bold"), width=10
            ).grid(row=0, column=4, padx=5, pady=5)

            refresh_list()

        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao abrir gerenciador de credenciais: {e}")


    def iniciar_processo():
        nonlocal resultado
        try:
            modo = modo_var.get()
            tipo_login = tipo_login_var.get()  # 'certificado' ou 'cpf_cnpj'
            
            if modo == "download":
                data_inicial = cal_inicial.get_date()
                data_final = cal_final.get_date()
                
                data_inicial_str = data_inicial.strftime('%d/%m/%Y')
                data_final_str = data_final.strftime('%d/%m/%Y')
                
                if data_inicial > data_final:
                    messagebox.showerror("Erro", "A data inicial deve ser anterior ou igual à data final!")
                    return
                
                if not resultado['diretorio_base']:
                    messagebox.showerror("Erro", "Selecione um diretório base!")
                    return
                
                # Valida tipo de login
                if tipo_login == 'certificado':
                    # Precisa ter certificados configurados
                    certs = load_certs(certs_path)
                    if not certs:
                        messagebox.showerror("Erro", "Nenhum certificado configurado. Use 'Gerenciar Certificados'.")
                        return
                else:  # cpf_cnpj
                    # Precisa ter credenciais configuradas
                    credenciais = load_credentials(credentials_path)
                    if not credenciais:
                        messagebox.showerror("Erro", "Nenhuma credencial CPF/CNPJ configurada. Use 'Gerenciar Credenciais'.")
                        return
                
                resultado['modo'] = 'download'
                resultado['data_inicial'] = data_inicial_str
                resultado['data_final'] = data_final_str
                resultado['tipo_login'] = tipo_login
                resultado['tipo_nota'] = tipo_nota_var.get()  # 'tomados' ou 'prestados'
                
            elif modo == "single_cert":
                # Processar apenas 1 certificado
                if tipo_login == 'certificado':
                    certificado_selecionado = combo_cert.get()
                    if not certificado_selecionado:
                        messagebox.showerror("Erro", "Selecione um certificado!")
                        return
                    resultado['certificado'] = certificado_selecionado
                    resultado['credencial'] = None
                else:
                    messagebox.showerror("Erro", "Modo 'single_cert' não suporta login por CPF/CNPJ. Selecione Certificado.")
                    return
                
                data_inicial = cal_inicial.get_date()
                data_final = cal_final.get_date()
                
                data_inicial_str = data_inicial.strftime('%d/%m/%Y')
                data_final_str = data_final.strftime('%d/%m/%Y')
                
                if data_inicial > data_final:
                    messagebox.showerror("Erro", "A data inicial deve ser anterior ou igual à data final!")
                    return
                
                if not resultado['diretorio_base']:
                    messagebox.showerror("Erro", "Selecione um diretório base!")
                    return
                
                resultado['modo'] = 'single_cert'
                resultado['data_inicial'] = data_inicial_str
                resultado['data_final'] = data_final_str
                resultado['tipo_login'] = tipo_login
                resultado['tipo_nota'] = tipo_nota_var.get()  # 'tomados' ou 'prestados'
                
            elif modo == "automatic":
                # Modo automático diário - permite selecionar data inicial opcional
                if not resultado['diretorio_base']:
                    messagebox.showerror("Erro", "Selecione um diretório base!")
                    return
                
                # Valida tipo de login
                if tipo_login == 'certificado':
                    certs = load_certs(certs_path)
                    if not certs:
                        messagebox.showerror("Erro", "Nenhum certificado configurado. Use 'Gerenciar Certificados'.")
                        return
                else:
                    credenciais = load_credentials(credentials_path)
                    if not credenciais:
                        messagebox.showerror("Erro", "Nenhuma credencial CPF/CNPJ configurada. Use 'Gerenciar Credenciais'.")
                        return
                
                resultado['modo'] = 'automatic'
                
                # Verifica se o usuário definiu uma data inicial (opcional)
                data_inicial_auto = cal_inicial_auto.get_date()
                hoje = datetime.now().date()
                
                # Se a data for anterior a hoje, usa como data inicial; caso contrário, usa padrão (últimos 30 dias)
                if data_inicial_auto and data_inicial_auto < hoje:
                    resultado['data_inicial'] = data_inicial_auto.strftime('%d/%m/%Y')
                
                # Hora diária (HH:MM)
                hora = entry_hora_auto.get().strip()
                if hora:
                    try:
                        datetime.strptime(hora, '%H:%M')
                    except Exception:
                        messagebox.showerror('Erro', 'Horário inválido. Use HH:MM (ex: 06:00).')
                        return
                    resultado['hora_automatico'] = hora
                resultado['tipo_login'] = tipo_login
                resultado['tipo_nota'] = tipo_nota_var.get()  # 'tomados' ou 'prestados'
                
            else:  # modo planilhar
                if not resultado['pasta_xmls']:
                    messagebox.showerror("Erro", "Selecione a pasta com os XMLs!")
                    return
                
                if not resultado['pasta_saida']:
                    messagebox.showerror("Erro", "Selecione a pasta de saída!")
                    return
                
                resultado['modo'] = 'planilhar'
                resultado['tipo_login'] = tipo_login
            
            root.quit()
            root.destroy()
            
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao processar configurações: {str(e)}")
    
    def cancelar():
        root.quit()
        root.destroy()
    
    def alternar_modo():
        modo = modo_var.get()
        if modo == "download":
            frame_datas.grid(row=4, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_diretorio_base.grid(row=5, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_tipo_login.grid(row=2, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_tipo_nota.grid(row=3, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_xmls.grid_remove()
            frame_saida.grid_remove()
            frame_cert_single.grid_remove()
            frame_hora_auto.grid_remove()
            # Habilitar campos de data
            cal_inicial.config(state='normal')
            cal_final.config(state='normal')
        elif modo == "single_cert":
            frame_datas.grid(row=4, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_diretorio_base.grid(row=5, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_tipo_login.grid(row=2, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_tipo_nota.grid(row=3, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_cert_single.grid(row=6, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_hora_auto.grid_remove()
            frame_xmls.grid_remove()
            frame_saida.grid_remove()
            # Habilitar campos de data
            cal_inicial.config(state='normal')
            cal_final.config(state='normal')
        elif modo == "automatic":
            frame_datas.grid(row=4, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_diretorio_base.grid(row=5, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_tipo_login.grid(row=2, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_tipo_nota.grid(row=3, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_cert_single.grid_remove()
            frame_hora_auto.grid_remove()
            frame_xmls.grid_remove()
            frame_saida.grid_remove()
            frame_hora_auto.grid(row=6, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            # Desabilitar campos de data
            cal_inicial.config(state='disabled')
            cal_final.config(state='disabled')
        else:
            frame_datas.grid_remove()
            frame_diretorio_base.grid_remove()
            frame_tipo_login.grid_remove()
            frame_tipo_nota.grid_remove()
            frame_cert_single.grid_remove()
            frame_hora_auto.grid_remove()
            frame_xmls.grid(row=2, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
            frame_saida.grid(row=3, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
        
        # Atualizar região de rolagem após mudar o modo
        root.update_idletasks()
        
        # Forçar atualização completa do canvas
        canvas.update_idletasks()
        scrollable_frame.update_idletasks()
        
        # Obtém a bounding box atual do conteúdo
        bbox = canvas.bbox("all")
        if bbox:
            canvas.configure(scrollregion=bbox)
        
        # Garantir que a janela seja grande o suficiente e centralizar
        if modo == "single_cert":
            # Aumentar a janela se necessário para o modo single_cert
            required_height = 700
            current_height = root.winfo_height()
            if current_height < required_height:
                root.geometry(f"{root.winfo_width()}x{required_height}")
            
            # Força atualização antes de rolar
            root.update_idletasks()
            canvas.update_idletasks()
            
            # Rolar para mostrar o frame de certificado - cálculo mais preciso
            # Obtém a posição Y do frame_cert_single
            try:
                # Usa o método grid_info para obter a posição
                cert_bbox = frame_cert_single.grid_bbox(column=0, row=0)
                if cert_bbox:
                    # Calcula a posição relativa de rolagem
                    total_height = canvas.bbox("all")[3]
                    if total_height > 0:
                        cert_y = cert_bbox[1]
                        # Rola para posição do certificado (com leve margem)
                        scroll_position = max(0, (cert_y - 50) / total_height)
                        canvas.yview_moveto(min(scroll_position, 1.0))
            except Exception:
                # Fallback: rolar para 80% do conteúdo
                canvas.yview_moveto(0.8)
    
    root = tk.Tk()
    root.title("Sistema NFS-e - Auditoria Fiscal")
    root.configure(bg='#f0f0f0')
    root.resizable(True, True)
    
    # Obter resolução da tela e calcular tamanho ideal da janela
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    
    # Define tamanho base baseado na resolução
    # Notebooks geralmente têm resoluções entre 1366x768 e 1920x1080
    if screen_width >= 1920 or screen_height >= 1080:
        # Telas Full HD ou maiores
        width, height = 760, 820
    elif screen_width >= 1366:
        # Telas HD padrão (notebooks comuns)
        width, height = 720, 760
    else:
        # Telas menores (netbooks, telas compactas)
        width, height = 680, 700
    
    # Define tamanho mínimo da janela
    root.minsize(680, 700)
    
    root.geometry(f"{width}x{height}")
    
    # Centralizar janela
    root.update_idletasks()
    width = root.winfo_width()
    height = root.winfo_height()
    x = (root.winfo_screenwidth() // 2) - (width // 2)
    y = (root.winfo_screenheight() // 2) - (height // 2)
    root.geometry(f'{width}x{height}+{x}+{y}')
    
    # Criar Canvas com barra de rolagem para permitir scrolling quando necessário
    canvas = tk.Canvas(root, bg='#f0f0f0', highlightthickness=0)
    scrollbar = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
    scrollable_frame = tk.Frame(canvas, bg='#f0f0f0')
    
    # Variável para armazenar o ID da janela do canvas
    canvas_window = None
    
    def update_scrollregion(event=None):
        """Atualiza a região de rolagem e ajusta largura do frame interno"""
        canvas.update_idletasks()
        # Atualiza largura do frame interno para acompanhar a janela
        if canvas_window:
            canvas_width = canvas.winfo_width()
            canvas.itemconfig(canvas_window, width=canvas_width)
        # Atualiza região de rolagem
        bbox = canvas.bbox("all")
        if bbox:
            canvas.configure(scrollregion=bbox)
    
    def create_window_callback(event):
        """Callback para criar janela do canvas"""
        nonlocal canvas_window
        canvas_width = canvas.winfo_width()
        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw", width=canvas_width)
    
    scrollable_frame.bind("<Configure>", update_scrollregion)
    canvas.bind("<Configure>", create_window_callback)
    
    canvas.configure(yscrollcommand=scrollbar.set)
    
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    
    # Permitir scrolling com a roda do mouse
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1*(event.delta/120)), "units")
    canvas.bind_all("<MouseWheel>", _on_mousewheel)
    
    # Frame principal dentro do frame rolável
    main_frame = tk.Frame(scrollable_frame, bg='#f0f0f0', padx=20, pady=20)
    main_frame.pack(fill="both", expand=True)
    
    title_label = tk.Label(main_frame, text="Sistema de Auditoria Fiscal NFS-e", 
                          font=("Arial", 16, "bold"), bg='#f0f0f0')
    title_label.grid(row=0, column=0, columnspan=2, pady=(0, 20))
    
    # Frame modo de operação
    frame_modo = tk.LabelFrame(main_frame, text="Modo de Operação", 
                               font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_modo.grid(row=1, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    
    modo_var = tk.StringVar(value="download")
    
    rb_download = tk.Radiobutton(frame_modo, text="Download e Processamento", 
                                 variable=modo_var, value="download", 
                                 command=alternar_modo, bg='#f0f0f0',
                                 font=("Arial", 10))
    rb_download.grid(row=0, column=0, padx=10, pady=5, sticky="w")
    
    rb_planilhar = tk.Radiobutton(frame_modo, text="Planilhar XMLs já baixados", 
                                  variable=modo_var, value="planilhar", 
                                  command=alternar_modo, bg='#f0f0f0',
                                  font=("Arial", 10))
    rb_planilhar.grid(row=1, column=0, padx=10, pady=5, sticky="w")

    rb_single_cert = tk.Radiobutton(frame_modo, text="Processar 1 certificado",
                                    variable=modo_var, value="single_cert",
                                    command=alternar_modo, bg='#f0f0f0',
                                    font=("Arial", 10))
    rb_single_cert.grid(row=2, column=0, padx=10, pady=5, sticky="w")

    rb_automatic = tk.Radiobutton(frame_modo, text="Modo automático diário",
                                  variable=modo_var, value="automatic",
                                  command=alternar_modo, bg='#f0f0f0',
                                  font=("Arial", 10))
    rb_automatic.grid(row=3, column=0, padx=10, pady=5, sticky="w")
    
    
    # Frame tipo de login (NOVA SEÇÃO)
    frame_tipo_login = tk.LabelFrame(main_frame, text="Tipo de Login no Portal NFS-e", 
                               font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_tipo_login.grid(row=2, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    
    tipo_login_var = tk.StringVar(value="certificado")
    tipo_nota_var = tk.StringVar(value="tomados")  # 'tomados' (Recebidas) ou 'prestados' (Emitidas)
    
    rb_login_cert = tk.Radiobutton(frame_tipo_login, text="Certificado Digital (PFX)", 
                                 variable=tipo_login_var, value="certificado", 
                                 bg='#f0f0f0',
                                 font=("Arial", 10))
    rb_login_cert.grid(row=0, column=0, padx=10, pady=5, sticky="w")
    
    rb_login_cpf = tk.Radiobutton(frame_tipo_login, text="CPF/CNPJ e Senha", 
                                  variable=tipo_login_var, value="cpf_cnpj", 
                                  bg='#f0f0f0',
                                  font=("Arial", 10))
    rb_login_cpf.grid(row=1, column=0, padx=10, pady=5, sticky="w")
    
    
    # Frame tipo de nota (Tomados/Prestados) - NOVA SEÇÃO
    frame_tipo_nota = tk.LabelFrame(main_frame, text="Tipo de Nota NFS-e", 
                               font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_tipo_nota.grid(row=3, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    
    rb_tomados = tk.Radiobutton(frame_tipo_nota, text="Tomados (Recebidas) - Serviços tomados de terceiros", 
                                 variable=tipo_nota_var, value="tomados", 
                                 bg='#f0f0f0',
                                 font=("Arial", 10))
    rb_tomados.grid(row=0, column=0, padx=10, pady=5, sticky="w")
    
    rb_prestados = tk.Radiobutton(frame_tipo_nota, text="Prestados (Emitidas) - Serviços prestados a terceiros", 
                                   variable=tipo_nota_var, value="prestados", 
                                   bg='#f0f0f0',
                                   font=("Arial", 10))
    rb_prestados.grid(row=1, column=0, padx=10, pady=5, sticky="w")
    
    
    # Frame horário (apenas modo automático)
    frame_hora_auto = tk.LabelFrame(main_frame, text="Agendamento (modo automático)", 
                                   font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_hora_auto.grid(row=5, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    
    # Campo de data inicial (opcional) para modo automático
    tk.Label(frame_hora_auto, text="Data inicial (opcional):", font=("Arial", 10), bg='#f0f0f0').grid(row=0, column=0, padx=5, pady=5, sticky='e')
    cal_inicial_auto = DateEntry(frame_hora_auto, width=12, background='darkblue',
                                foreground='white', borderwidth=2, date_pattern='dd/mm/yyyy')
    cal_inicial_auto.grid(row=0, column=1, padx=5, pady=5, sticky='w')
    tk.Label(frame_hora_auto, text="(deixe em branco para últimos 30 dias)", font=("Arial", 8), bg='#f0f0f0', fg='#666').grid(row=0, column=2, padx=5, pady=5, sticky='w')
    
    # Campo de horário
    tk.Label(frame_hora_auto, text="Horário diário (HH:MM):", font=("Arial", 10), bg='#f0f0f0').grid(row=1, column=0, padx=5, pady=5, sticky='e')
    entry_hora_auto = ttk.Entry(frame_hora_auto, width=10)
    entry_hora_auto.insert(0, "06:00")
    entry_hora_auto.grid(row=1, column=1, padx=5, pady=5, sticky='w')
    
    # começa escondido; será exibido no alternar_modo()
    frame_hora_auto.grid_remove()

    # Frame datas (para download)
    frame_datas = tk.LabelFrame(main_frame, text="Período de Download", 
                               font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_datas.grid(row=4, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    
    tk.Label(frame_datas, text="Data Inicial:", font=("Arial", 10), 
            bg='#f0f0f0').grid(row=0, column=0, padx=5, pady=5, sticky='e')
    cal_inicial = DateEntry(frame_datas, width=12, background='darkblue',
                           foreground='white', borderwidth=2, date_pattern='dd/mm/yyyy')
    cal_inicial.grid(row=0, column=1, padx=5, pady=5, sticky='w')
    
    tk.Label(frame_datas, text="Data Final:", font=("Arial", 10), 
            bg='#f0f0f0').grid(row=1, column=0, padx=5, pady=5, sticky='e')
    cal_final = DateEntry(frame_datas, width=12, background='darkblue',
                         foreground='white', borderwidth=2, date_pattern='dd/mm/yyyy')
    cal_final.set_date(datetime.now())
    cal_final.grid(row=1, column=1, padx=5, pady=5, sticky='w')
    
    # Frame diretório base (para download)
    frame_diretorio_base = tk.LabelFrame(main_frame, text="Diretório Base para Salvar Arquivos", 
                                       font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_diretorio_base.grid(row=5, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    
    # Botões do modo download
    frame_botoes_download = tk.Frame(frame_diretorio_base, bg='#f0f0f0')
    frame_botoes_download.pack(pady=5)

    btn_selecionar_dir = tk.Button(
        frame_botoes_download, text="Selecionar Diretório",
        command=selecionar_diretorio_base, bg='#2196F3', fg='white',
        font=("Arial", 10, "bold"), width=20
    )
    btn_selecionar_dir.pack(side="left", padx=6)

    btn_gerenciar_certs = tk.Button(
        frame_botoes_download, text="Gerenciar Certificados",
        command=gerenciar_certificados, bg='#6A1B9A', fg='white',
        font=("Arial", 10, "bold"), width=22
    )
    btn_gerenciar_certs.pack(side="left", padx=6)

    btn_gerenciar_creds = tk.Button(
        frame_botoes_download, text="Gerenciar Credenciais",
        command=gerenciar_credenciais, bg='#009688', fg='white',
        font=("Arial", 10, "bold"), width=20
    )
    btn_gerenciar_creds.pack(side="left", padx=6)
    
    label_diretorio_base = tk.Label(frame_diretorio_base, text="Nenhum diretório selecionado", 
                                   font=("Arial", 9), bg='white', relief=tk.SUNKEN, 
                                   anchor=tk.W, padx=5, pady=5, wraplength=500)
    label_diretorio_base.pack(fill="x", pady=5)
    
    # Frame pasta XMLs (para planilhar)
    frame_xmls = tk.LabelFrame(main_frame, text="Pasta com XMLs já Baixados", 
                              font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_xmls.grid(row=2, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    frame_xmls.grid_remove()
    
    btn_selecionar_xmls = tk.Button(frame_xmls, text="Selecionar Pasta com XMLs", 
                                    command=selecionar_pasta_xmls, bg='#2196F3', fg='white',
                                    font=("Arial", 10, "bold"), width=20)
    btn_selecionar_xmls.pack(pady=5)
    
    label_pasta_xmls = tk.Label(frame_xmls, text="Nenhuma pasta selecionada", 
                               font=("Arial", 9), bg='white', relief=tk.SUNKEN, 
                               anchor=tk.W, padx=5, pady=5, wraplength=400)
    label_pasta_xmls.pack(fill="x", pady=5)
    
    # Frame pasta saída (para planilhar)
    frame_saida = tk.LabelFrame(main_frame, text="Pasta para Salvar Planilha", 
                               font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_saida.grid(row=3, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    frame_saida.grid_remove()
    
    btn_selecionar_saida = tk.Button(frame_saida, text="Selecionar Pasta de Saída", 
                                     command=selecionar_pasta_saida, bg='#2196F3', fg='white',
                                     font=("Arial", 10, "bold"), width=20)
    btn_selecionar_saida.pack(pady=5)
    
    label_pasta_saida = tk.Label(frame_saida, text="Nenhuma pasta selecionada", 
                                font=("Arial", 9), bg='white', relief=tk.SUNKEN, 
                                anchor=tk.W, padx=5, pady=5, wraplength=400)
    label_pasta_saida.pack(fill="x", pady=5)

    # Frame certificado único (para single_cert)
    frame_cert_single = tk.LabelFrame(main_frame, text="Selecionar Certificado",
                                       font=("Arial", 11, "bold"), bg='#f0f0f0', padx=10, pady=10)
    frame_cert_single.grid(row=5, column=0, columnspan=2, pady=10, padx=20, sticky="ew")
    frame_cert_single.grid_remove()

    # Carregar lista de certificados
    settings = get_settings()
    certs_path = str(settings.certs_json_path)
    credentials_path = str(settings.credentials_json_path)
    certs = load_certs(certs_path)
    cert_aliases = [c.get("alias", "") for c in certs if c.get("alias")]

    tk.Label(frame_cert_single, text="Certificado:", font=("Arial", 10),
             bg='#f0f0f0').grid(row=0, column=0, padx=5, pady=5, sticky='e')

    combo_cert = ttk.Combobox(frame_cert_single, values=cert_aliases, state='readonly', width=40)
    combo_cert.grid(row=0, column=1, padx=5, pady=5, sticky='ew')
    
    # Configurar expansão da coluna 1 para preencher o espaço disponível
    frame_cert_single.columnconfigure(1, weight=1)
    if cert_aliases:
        combo_cert.current(0)

    # Frame botões
    frame_botoes = tk.Frame(main_frame, bg='#f0f0f0')
    frame_botoes.grid(row=6, column=0, columnspan=2, pady=20)
    
    btn_iniciar = tk.Button(frame_botoes, text="Iniciar Processo", 
                           command=iniciar_processo, bg='#4CAF50', fg='white',
                           font=("Arial", 11, "bold"), width=20, height=2)
    btn_iniciar.grid(row=0, column=0, padx=10)
    
    btn_cancelar = tk.Button(frame_botoes, text="Cancelar", 
                            command=cancelar, bg='#f44336', fg='white',
                            font=("Arial", 11, "bold"), width=20, height=2)
    btn_cancelar.grid(row=0, column=1, padx=10)
    
    footer_label = tk.Label(main_frame, text="Selecione o modo de operação e configure os parâmetros", 
                           font=("Arial", 9), bg='#f0f0f0', fg='#666')
    footer_label.grid(row=7, column=0, columnspan=2, pady=10)
    
    # Configurar expansão de colunas
    main_frame.columnconfigure(0, weight=1)
    main_frame.columnconfigure(1, weight=1)
    
    root.lift()
    root.focus_force()
    root.mainloop()
    
    return resultado
