-- migrations.sql
-- Execute este arquivo no seu PostgreSQL (uma vez) para garantir o schema mínimo.

-- 1) Estado de execução (continuar de onde parou)
CREATE TABLE IF NOT EXISTS nfse_run_state (
  cert_alias TEXT PRIMARY KEY,
  last_processed_date DATE,
  last_run_at TIMESTAMP,
  status TEXT,
  last_error TEXT
);

-- 2) Cache CNPJ (somente Postgres)
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
ALTER TABLE cnpj_cache ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_cnpj_cache_expiracao ON cnpj_cache (data_expiracao);

-- 3) Notas NFS-e: dedupe por cert_alias + chave_nfse + processo_id support
-- Se a tabela já existir no seu banco, os ALTERs abaixo só adicionam o que falta.
CREATE TABLE IF NOT EXISTS nfse_notas (
  id BIGSERIAL PRIMARY KEY,
  cert_alias TEXT NOT NULL,
  processo_id UUID,
  chave_nfse TEXT NOT NULL,
  numero_documento TEXT,
  competencia TEXT,
  data_emissao DATE,
  municipio TEXT,
  cnpj_prestador TEXT,
  razao_social TEXT,
  valor_total NUMERIC,
  valor_bc NUMERIC,
  valor_liquido NUMERIC,
  csrf NUMERIC,
  irrf NUMERIC,
  percentual_irrf NUMERIC,
  inss NUMERIC,
  iss NUMERIC,
  retencao_csrf TEXT,
  incidencia_iss TEXT,
  data_pagamento TEXT,
  codigo_servico TEXT,
  descricao_servico TEXT,
  codigo_nbs TEXT,
  codigo_cnae TEXT,
  descricao_cnae TEXT,
  simples_xml TEXT,
  consulta_simples_api TEXT,
  status_simples_nacional TEXT,
  status_csrf TEXT,
  status_irrf TEXT,
  status_inss TEXT,
  status_base_calculo TEXT,
  alertas_fiscais TEXT,
  dados_completos JSONB NOT NULL,
  arquivo_origem TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now()
);

ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS cert_alias TEXT;
ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS processo_id UUID;
ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS chave_nfse TEXT;
ALTER TABLE nfse_notas ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS ux_nfse_notas_cert_chave ON nfse_notas (cert_alias, chave_nfse);
CREATE INDEX IF NOT EXISTS idx_nfse_notas_processo ON nfse_notas (processo_id);

-- 4) Processos NFS-e
CREATE TABLE IF NOT EXISTS nfse_processos (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  execution_id UUID,
  cert_alias TEXT NOT NULL,
  login_type TEXT,
  tipo_nota TEXT,
  start_date DATE,
  end_date DATE,
  status TEXT DEFAULT 'queued',
  created_at TIMESTAMP DEFAULT now(),
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  total_notas INTEGER DEFAULT 0,
  total_xml INTEGER DEFAULT 0,
  total_pdf INTEGER DEFAULT 0,
  total_corretas INTEGER DEFAULT 0,
  total_divergentes INTEGER DEFAULT 0,
  error_message TEXT
);
CREATE INDEX idx_nfse_processos_cert ON nfse_processos (cert_alias);
CREATE INDEX idx_nfse_processos_status ON nfse_processos (status);
CREATE INDEX idx_nfse_processos_dates ON nfse_processos (start_date, end_date);

-- 5) Execuções (migra JOBS in-memory)
CREATE TABLE IF NOT EXISTS nfse_execucoes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id TEXT UNIQUE,
  processo_id UUID,
  payload_json JSONB,
  status TEXT DEFAULT 'queued',
  created_at TIMESTAMP DEFAULT now(),
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  error TEXT,
  traceback TEXT
);
CREATE INDEX idx_nfse_execucoes_job ON nfse_execucoes (job_id);
CREATE INDEX idx_nfse_execucoes_processo ON nfse_execucoes (processo_id);

-- 6) Arquivos por processo
CREATE TABLE IF NOT EXISTS nfse_processo_arquivos (
  id BIGSERIAL PRIMARY KEY,
  processo_id UUID NOT NULL REFERENCES nfse_processos(id) ON DELETE CASCADE,
  tipo_arquivo TEXT NOT NULL CHECK (tipo_arquivo IN ('pdf', 'xml', 'relatorio')),
  nome_arquivo TEXT NOT NULL,
  storage_key TEXT,
  caminho_local TEXT,
  content_type TEXT,
  tamanho_bytes BIGINT,
  competencia TEXT,
  created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX idx_nfse_arquivos_processo_tipo ON nfse_processo_arquivos (processo_id, tipo_arquivo);
CREATE INDEX idx_nfse_arquivos_competencia ON nfse_processo_arquivos (competencia);
