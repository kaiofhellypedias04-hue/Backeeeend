# Deploy no Render

## Resumo do que foi alterado

- A API deixou de importar `main.py`, eliminando a dependencia indireta de `modules.gui`, `tkinter` e `tkcalendar` no startup do FastAPI.
- As leituras de `certs.json` e `credentials.json` foram movidas para `modules/config_loader.py`.
- Configuracoes, caminhos e validacoes de ambiente foram centralizados em `modules/settings.py`.
- O armazenamento de senhas passou a usar prioridade `env vars -> arquivo local de segredos -> keyring opcional`, por meio de `modules/secret_store.py`.
- `api.py`, `cli.py`, Playwright e worker agora usam os mesmos caminhos e configuracoes centralizados.
- O startup valida `DATABASE_URL`, garante a extensao `pgcrypto` e cria os schemas usados pela aplicacao.

## Como rodar localmente

1. Crie um ambiente virtual Python.
2. Instale as dependencias Python:
   `pip install -r requirements.txt`
3. Instale as dependencias Node:
   `npm ci`
4. Instale o navegador do Playwright:
   `npx playwright install chromium`
5. Copie `.env.example` para `.env` e ajuste as variaveis.
6. Garanta que o PostgreSQL esteja acessivel em `DATABASE_URL`.
7. Suba a API:
   `uvicorn api:app --host 0.0.0.0 --port 8000`

## Como configurar no Render

1. Crie um Web Service usando `Docker`.
2. Aponte para este projeto.
3. Configure um disco persistente e monte em `/var/data/backend`.
4. Use os caminhos definidos em `render.yaml` para runtime e segredos.
5. Defina `DATABASE_URL` usando o Postgres do Render.
6. Defina `CORS_ORIGINS` com os dominios reais do frontend.
7. Garanta health check em `/health` se nao usar o `render.yaml`.
8. Faca o deploy.

## Variaveis de ambiente

Obrigatorias:

- `DATABASE_URL`

Recomendadas em producao:

- `APP_ENV=production`
- `APP_DATA_DIR`
- `DATA_DIR`
- `OUTPUT_DIR`
- `TEMP_DIR`
- `CERTS_DIR`
- `CERTS_JSON_PATH`
- `CREDENTIALS_JSON_PATH`
- `SECRETS_FILE_PATH`
- `CORS_ORIGINS`
- `ENABLE_KEYRING_FALLBACK=false`

Opcionais:

- `DB_SSLMODE`
- `DB_CONNECT_TIMEOUT`
- `PLAYWRIGHT_TIMEOUT_MS`
- `NODE_BIN`
- `NPM_BIN`
- `PLAYWRIGHT_SCRIPT_PATH`
- `PACKAGE_JSON_PATH`
- `CERT_PASSWORDS_JSON`
- `CREDENTIAL_PASSWORDS_JSON`
- `INVERTEXTO_TOKEN`
- `INVERTEXTO_RPM`
- `INVERTEXTO_DELAY_SECONDS`
- `S3_ENDPOINT`
- `S3_BUCKET`
- `S3_ACCESS_KEY`
- `S3_SECRET_KEY`
- `S3_REGION`

## PostgreSQL

- O backend exige `DATABASE_URL` ja no startup.
- A conexao aceita `postgresql://` e tambem normaliza `postgres://`.
- Se `DB_SSLMODE` for informado, ele e aplicado automaticamente a connection string.
- O startup garante `pgcrypto` antes de criar tabelas que usam `gen_random_uuid()`.
- Nao existe dependencia implicita de banco local.

## Migrations / schema

- O projeto mantem a estrategia atual de criacao de schema por codigo.
- O arquivo `migrations.sql` foi preservado para uso manual quando desejado.
- Se quiser aplicar manualmente:
  `psql "$DATABASE_URL" -f migrations.sql`

## Playwright / Node

- O deploy foi preparado para Docker porque o projeto depende de Python + Node + Playwright.
- O `Dockerfile` instala Node 20, dependencias NPM e o navegador Chromium do Playwright.
- O backend trata erros de `node` ausente, timeout, script ausente, `package.json` ausente, browser ausente e exit code diferente de zero.
- `node_modules/` nao entra no projeto final; ele e reconstruido no build do container.

## Runtime e armazenamento

- Diretorios de runtime sao criados automaticamente.
- Outputs e temporarios usam env vars e disco persistente.
- Segredos podem vir por env vars, arquivo runtime ou keyring opcional.
- O arquivo `data/RETENCOES_REGRAS.xlsx` faz parte da aplicacao e precisa seguir no build.

## O que foi removido / limpo

- `.env`
- `node_modules/`
- `__pycache__/`
- logs `worker_*.log`
- diretorios de saida e temporarios gerados em runtime
- artefatos locais sensiveis como `certs.json`, `credentials.json` e certificados `.pfx`

## Comando de start

`uvicorn api:app --host 0.0.0.0 --port $PORT`

## Estrutura basica apos o ajuste

- `api.py`: aplicacao FastAPI pronta para Render.
- `modules/settings.py`: settings e validacao de ambiente.
- `modules/config_loader.py`: carregamento neutro de certificados e credenciais.
- `modules/secret_store.py`: resolucao de segredos cloud-safe.
- `modules/cert_manager.py`: operacoes de certificados e credenciais usando a nova camada de segredos.
- `Dockerfile`: build completo para Python + Node + Playwright.
- `render.yaml`: configuracao sugerida para o Render.
- `.env.example`: template limpo de variaveis.
