# Auditoria NFS-e (Portal Nacional) — GUI + Automático

Este projeto baixa NFS-e (XML/PDF) do Portal Nacional via Playwright (Node.js), processa os XMLs, consulta dados de CNPJ (com cache em Postgres) e mantém planilhas **mensais incrementais** sem duplicar.

## Requisitos

- **Python 3.10+**
- **Node.js 18+** (recomendado)
- **PostgreSQL 13+**

## Instalação

### 1) Python

```bash
python -m venv .venv
.
# Windows
.venv\\Scripts\\activate

# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
```

### 2) Node / Playwright

```bash
npm install
npx playwright install
```

## Configuração do Postgres

1) Crie um banco e usuário no Postgres.
2) Configure a variável `DATABASE_URL` no arquivo `.env` (na raiz do projeto, mesma pasta do `main.py`):

Exemplo:

```env
DATABASE_URL=postgresql://usuario:senha@localhost:5432/seu_banco
```

3) Rode o schema/migration:

```bash
psql "$DATABASE_URL" -f migrations.sql
```

> Observação: o sistema também tenta criar/ajustar tabelas automaticamente no primeiro uso, mas o recomendado é aplicar o `migrations.sql`.

## Certificados

- O arquivo `certs.json` deve conter uma lista com `{ "alias": "...", "pfxPath": "..." }`.
- Você pode usar a GUI (menu **Gerenciar Certificados**) para cadastrar e salvar senha no cofre (keyring).
- Para referência, veja `certs.example.json`.

## Modo Manual (GUI)

```bash
python main.py
```

Na GUI você escolhe:

- Diretório base
- Intervalo de datas
- Gerenciar certificados

O sistema cria/usa a estrutura:

```
<BASE>/
  <CERT_ALIAS>/
    <YYYY>/
      <MM>/
        xml/
        pdf/
        planilhas/
    tmp_downloads/
```

## Modo Automático (CLI / headless)

Entrada principal: `cli.py`

### Exemplos

Processar todos os certificados (primeira execução com start obrigatório):

```bash
python cli.py --modo automatico --base-dir "C:\\NFSE" --certificados todos --start 2026-01-01 --headless true
```

Processar apenas alguns certificados:

```bash
python cli.py --modo automatico --base-dir "/data/nfse" --certificados alias1,alias2 --headless true
```

### Regras do automático

- Se **não** passar `--start`, o sistema tenta continuar a partir do `nfse_run_state.last_processed_date + 1`.
- Se não houver estado ainda, usa fallback seguro (`hoje-29`).
- `--end` default = hoje.
- O intervalo é quebrado em chunks de até `--chunk-days` (default 30) para respeitar a janela do Portal.

## Agendamento

### Windows (Task Scheduler)

- Aponte para o Python da venv (ou python do sistema)
- Configure a pasta de trabalho como a raiz do projeto
- Comando exemplo:

```bat
cmd /c "C:\\caminho\\projeto\\.venv\\Scripts\\python.exe cli.py --modo automatico --base-dir C:\\NFSE --certificados todos --headless true"
```

### Linux (cron)

Exemplo diário às 03:00:

```bash
0 3 * * * /caminho/projeto/.venv/bin/python /caminho/projeto/cli.py --modo automatico --base-dir /data/nfse --certificados todos --headless true >> /var/log/nfse.log 2>&1
```

## Deploy em Railway + Supabase

- Configure `DATABASE_URL` com a string do Supabase.
- Configure `DB_SSLMODE=require`.
- Monte um volume persistente e aponte `APP_DATA_DIR=/data/backend`.
- Defina `CORS_ORIGINS` com os dominios reais do frontend, separados por virgula.
- Comando de start:

```bash
gunicorn api:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
```
