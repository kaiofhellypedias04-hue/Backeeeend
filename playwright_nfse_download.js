// playwright_nfse_download.js
// Login + download NFS-e (XML e DANFS-e) com certificado digital (PFX) ou CPF/CNPJ + Senha

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const CERT_ORIGINS = ['https://www.nfse.gov.br', 'https://nfse.gov.br'];

const LOGIN_URL =
  'https://www.nfse.gov.br/EmissorNacional/Login?ReturnUrl=%2fEmissorNacional%2f';

// URLs para notas - definido dinamicamente com base no tipoNota
let NOTAS_URL = '/EmissorNacional/Notas/Recebidas';  // padrão: tomados
let NOTAS_URL_CHECK = '/notas/recebidas';  // para verificação em lower case

const KEEP_ALIVE_INTERVAL_MS = 30000; // Reduzido de 45000 para 30 segundos
const CHECKPOINT_FILE = 'download_checkpoint.json';
const MAX_RELOGIN_ATTEMPTS = 3; // Aumentado de 1 para 3 tentativas
const RELOGIN_INTERVAL_MS = 90000; // Reduzido de 120000 (2min) para 90 segundos
const SPLIT_THRESHOLD = 800; // Se mais de 800 notas, dividir período
const SPLIT_DAYS = 15; // (legacy) mantido apenas para compatibilidade
const MAX_SPLIT_DEPTH = 12; // Limite de recursão para split interno
const PAUSE_EVERY_DOWNLOADS = 100;
const PAUSE_MIN_MS = 60_000;
const PAUSE_MAX_MS = 70_000;

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const val = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : 'true';
      args[key] = val;
    }
  }
  return args;
}

function pickArg(args, keys) {
  for (const k of keys) {
    const v = args[k];
    if (v !== undefined && v !== null && String(v).trim() !== '') return v;
  }
  return undefined;
}

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function readCerts(certsJsonPathFromArgs) {
  const envPath = process.env.CERTS_JSON;
  const p = certsJsonPathFromArgs || envPath;
  if (!p || !fs.existsSync(p)) {
    throw new Error(
      'CERTS_JSON/certsJson não encontrado.'
    );
  }
  const txt = fs.readFileSync(p, 'utf-8');
  const data = JSON.parse(txt);
  if (!Array.isArray(data) || data.length === 0) throw new Error('certs.json inválido');

  return data.map((c) => ({
    alias: c.alias,
    pfxPath: c.pfxPath || c.pfx_path,
    passEnv: c.passEnv || c.pass_env || c.password_env,
    cnpj: c.cnpj || null,
  }));
}

function readCredentials(credentialsJsonPathFromArgs) {
  const envPath = process.env.CREDENTIALS_JSON;
  const p = credentialsJsonPathFromArgs || envPath;
  if (!p || !fs.existsSync(p)) {
    throw new Error(
      'CREDENTIALS_JSON/credentialsJson não encontrado.'
    );
  }
  const txt = fs.readFileSync(p, 'utf-8');
  const data = JSON.parse(txt);
  if (!Array.isArray(data) || data.length === 0) throw new Error('credentials.json inválido');

  return data.map((c) => ({
    alias: c.alias,
    cpf_cnpj: c.cpf_cnpj || c.cpfcnpj || c.cnpj || '',
  }));
}

async function safeWaitNetworkIdle(page, timeoutMs = 30000) {
  await page.waitForLoadState('networkidle', { timeout: timeoutMs }).catch(() => {});
}

function getCheckpointPath(downloadDir) {
  return path.join(downloadDir, '..', CHECKPOINT_FILE);
}

function saveCheckpoint(downloadDir, data) {
  try {
    const checkpointPath = getCheckpointPath(downloadDir);
    fs.writeFileSync(checkpointPath, JSON.stringify(data, null, 2), 'utf-8');
  } catch (e) {
    console.error('Erro ao salvar checkpoint:', e.message);
  }
}

function loadCheckpoint(downloadDir) {
  try {
    const checkpointPath = getCheckpointPath(downloadDir);
    if (fs.existsSync(checkpointPath)) {
      const data = JSON.parse(fs.readFileSync(checkpointPath, 'utf-8'));
      return data;
    }
  } catch (e) {
    console.error('Erro ao carregar checkpoint:', e.message);
  }
  return null;
}

function clearCheckpoint(downloadDir) {
  try {
    const checkpointPath = getCheckpointPath(downloadDir);
    if (fs.existsSync(checkpointPath)) {
      fs.unlinkSync(checkpointPath);
    }
  } catch (e) {
    console.error('Erro ao limpar checkpoint:', e.message);
  }
}

async function isSessionExpired(page) {
  const url = page.url().toLowerCase();
  const bodyText = (await page.locator('body').innerText().catch(() => '')).toLowerCase();

  if (url.includes('/emissornacional/login')) {
    console.log('   ⚠️ Sessão expirada: Está na página de Login');
    return true;
  }

  const isNotasPage = url.includes(NOTAS_URL_CHECK);
  const isResultsPage = url.includes(NOTAS_URL_CHECK) && (
    bodyText.includes('nenhum registro') ||
    bodyText.includes('nenhum resultado') ||
    bodyText.includes('não há dados') ||
    bodyText.includes('sem resultados') ||
    bodyText.includes('nenhuma nota')
  );
  
  if (!isNotasPage && !isResultsPage) {
    console.log('   ⚠️ Sessão expirada: URL não é ' + NOTAS_URL);
    return true;
  }

  if (isResultsPage) {
    return false;
  }

  const hasTotalText = /total\s+de\s+\d+\s+registros/i.test(bodyText);
  const hasTable = await page.locator('xpath=//table//tbody/tr').count().catch(() => 0);
  
  if (!hasTotalText && hasTable === 0) {
    const semDadosMsg = bodyText.includes('nenhum') || bodyText.includes('não há') || bodyText.includes('sem');
    if (semDadosMsg) {
      return false;
    }
    return true;
  }

  return false;
}

let keepAliveInterval = null;
let reloginInterval = null;
let lastActivityTime = Date.now();

function startKeepAlive(page) {
  if (keepAliveInterval) clearInterval(keepAliveInterval);

  let keepAliveRunning = false;
  let lastLogAt = 0;
  let actionCount = 0;

  keepAliveInterval = setInterval(async () => {
    if (keepAliveRunning) return;
    if (!page || page.isClosed()) return;

    keepAliveRunning = true;
    try {
      actionCount++;
      
      // Ações variadas para imitar comportamento humano
      const actionType = actionCount % 4;
      
      switch(actionType) {
        case 0:
          // Scroll suave para baixo
          await page.evaluate(() => {
            try { 
              window.scrollBy(0, Math.floor(Math.random() * 50) + 20); 
            } catch (_) {}
          });
          break;
        case 1:
          // Scroll suave para cima
          await page.evaluate(() => {
            try { 
              window.scrollBy(0, -Math.floor(Math.random() * 50) - 20); 
            } catch (_) {}
          });
          break;
        case 2:
          // Movimento de mouse aleatório
          const box = await page.viewportSize();
          const x = Math.floor(Math.random() * (box?.width || 800));
          const y = Math.floor(Math.random() * (box?.height || 600));
          await page.mouse.move(x, y).catch(() => {});
          break;
        case 3:
          // Clique leve em algum elemento da página
          await page.evaluate(() => {
            try {
              // Move o mouse para um elemento aleatório
              const elements = document.querySelectorAll('div, span, a, button');
              if (elements.length > 0) {
                const randomEl = elements[Math.floor(Math.random() * elements.length)];
                randomEl.dispatchEvent(new MouseEvent('mousemove', { bubbles: true }));
              }
            } catch (_) {}
          }).catch(() => {});
          break;
      }

      // Log controlado (a cada ~3 min)
      const now = Date.now();
      if (now - lastLogAt > 180000) {
        console.log('   💓 Keep-alive: ação humana simulada');
        lastLogAt = now;
      }
    } catch (_) {
      // ignora
    } finally {
      keepAliveRunning = false;
    }
  }, KEEP_ALIVE_INTERVAL_MS);
}

function stopKeepAlive() {
  if (keepAliveInterval) {
    clearInterval(keepAliveInterval);
    keepAliveInterval = null;
  }
}

function startReloginTimer(page, reloginCallback) {
  if (reloginInterval) {
    clearInterval(reloginInterval);
  }
  reloginInterval = setInterval(async () => {
    const timeSinceActivity = Date.now() - lastActivityTime;
    if (timeSinceActivity >= RELOGIN_INTERVAL_MS) {
      console.log(`   ⏰ 2 minutos sem atividade, fazendo re-login preventivo...`);
      try {
        await reloginCallback();
        lastActivityTime = Date.now();
      } catch (e) {
        console.log(`   ❌ Erro no re-login preventivo: ${e.message}`);
      }
    }
  }, 30000); // Verifica a cada 30 segundos
}

function stopReloginTimer() {
  if (reloginInterval) {
    clearInterval(reloginInterval);
    reloginInterval = null;
  }
}

function updateActivity() {
  lastActivityTime = Date.now();
}

async function clickAndSaveDownload(page, clickableLocator, downloadDir) {
  const el = clickableLocator.first();
  const visible = await el.isVisible().catch(() => false);
  if (!visible) return null;

  updateActivity(); // Marca atividade

  const downloadPromise = page.waitForEvent('download', { timeout: 45000 }).catch(() => null);
  const popupPromise = page.waitForEvent('popup', { timeout: 45000 }).catch(() => null);

  await el.click({ timeout: 30000 });

  const winner = await Promise.race([downloadPromise, popupPromise]);

  let download = null;

  if (winner && typeof winner.saveAs === 'function') {
    download = winner;
  } else if (winner && typeof winner.waitForEvent === 'function') {
    const popup = winner;
    download = await popup.waitForEvent('download', { timeout: 45000 }).catch(() => null);
    await popup.close().catch(() => {});
  } else {
    return null;
  }

  const suggested = download.suggestedFilename();
  const target = path.join(downloadDir, suggested);
  if (fs.existsSync(target)) {
    console.log(`   ↩️  Já existe, pulando: ${suggested}`);
    await download.delete().catch(() => {});
    return null;
  }
  await download.saveAs(target);
  return target;
}

function assertNotOnLogin(url) {
  const u = String(url || '').toLowerCase();
  if (u.includes('/emissornacional/login') || u.includes('/emissornacional/login/index')) {
    throw new Error(`Falha no login: portal redirecionou para Login. URL atual: ${url}`);
  }
  if (u.includes('/emissornacional/certificado')) {
    throw new Error(
      `Falha no login: portal ficou na etapa /Certificado e não autenticou. URL atual: ${url}`
    );
  }
}

async function loginWithCredentials(page, cpfCnpj, senha) {
  console.log('   🔐 Fazendo login com CPF/CNPJ e Senha...');
  
  await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 120000 });
  await safeWaitNetworkIdle(page, 60000);
  
  await page.waitForSelector('#Inscricao', { timeout: 60000 });
  await page.waitForSelector('#Senha', { timeout: 60000 });
  await page.waitForSelector('button[type="submit"]', { timeout: 60000 });
  
  await page.fill('#Inscricao', cpfCnpj);
  await page.fill('#Senha', senha);
  await page.click('button[type="submit"]');
  
  await safeWaitNetworkIdle(page, 60000);
  await page.waitForTimeout(2000);
  
  const url = page.url().toLowerCase();
  
  if (url.includes('/emissornacional/login')) {
    throw new Error('Falha no login: portal redirecionou para Login após tentativa.');
  }
  
  const notasLink = page.locator(`a[href="${NOTAS_URL}"]`).first();
  const logou = await notasLink.isVisible().catch(() => false);
  
  if (!logou) {
    const shot = path.join(downloadDir, `login_falhou_${Date.now()}.png`);
    await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
    throw new Error(`Login não confirmado (link "${NOTAS_URL}" não apareceu).`);
  }
  
  console.log('   ✅ Login com CPF/CNPJ realizado com sucesso!');
  updateActivity();
  return true;
}

async function reloginWithCert(page, cert, dataInicial, dataFinal) {
  console.log('   🔄 Fazendo re-login com certificado...');

  await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 120000 });
  await safeWaitNetworkIdle(page, 60000);

  const certLink = page.locator('a[href="/EmissorNacional/Certificado"]').first();
  await certLink.waitFor({ state: 'attached', timeout: 60000 });
  await certLink.scrollIntoViewIfNeeded().catch(() => {});
  await Promise.allSettled([
    page.waitForNavigation({ timeout: 120000, waitUntil: 'domcontentloaded' }),
    certLink.click({ timeout: 60000, force: true }),
  ]);
  await safeWaitNetworkIdle(page, 60000);

  assertNotOnLogin(page.url());

  const notasLink = page.locator(`a[href="${NOTAS_URL}"]`).first();
  const logou = await notasLink.isVisible().catch(() => false);
  if (!logou) {
    throw new Error(`Re-login falhou: link "${NOTAS_URL}" não apareceu`);
  }

  await Promise.allSettled([
    page.waitForNavigation({ timeout: 120000, waitUntil: 'domcontentloaded' }),
    notasLink.click({ timeout: 60000 }),
  ]);
  await safeWaitNetworkIdle(page, 60000);
  assertNotOnLogin(page.url());

  await applyDateFilterIfNeeded(page, dataInicial, dataFinal);

  console.log('   ✅ Re-login com certificado realizado');
  updateActivity();
}

async function reloginWithCredentials(page, credencial, senha, dataInicial, dataFinal) {
  console.log('   🔄 Fazendo re-login com CPF/CNPJ...');

  await loginWithCredentials(page, credencial.cpf_cnpj, senha);
  await applyDateFilterIfNeeded(page, dataInicial, dataFinal);

  console.log('   ✅ Re-login com CPF/CNPJ realizado');
  updateActivity();
}

async function applyDateFilterIfNeeded(page, startStr, endStr) {
  // Abre o filtro pela mesma UI existente no portal (sem page.goto/refresh)
  const btnFiltro = page.locator(
    `xpath=//button[contains(@class,'btn') and (contains(.,'Filtro') or contains(.,'Filtrar'))] | //a[contains(@class,'btn') and (contains(.,'Filtro') or contains(.,'Filtrar'))] | //i[contains(@class,'fa-filter')]/ancestor::*[self::a or self::button][1] | //button[contains(@title,'Filtro') or contains(@aria-label,'Filtro')] | //a[contains(@title,'Filtro') or contains(@aria-label,'Filtro')]`
  ).first();

  const campoInicial = page.locator('#datainicio');
  const campoFinal = page.locator('#datafim');

  // alguns layouts mostram os inputs só após abrir modal/painel de filtro
  const inputsVisiveis = await campoInicial.isVisible().catch(() => false);
  if (!inputsVisiveis) {
    await btnFiltro.click({ timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(250);
  }

  await campoInicial.waitFor({ state: 'visible', timeout: 60000 });
  await campoFinal.waitFor({ state: 'visible', timeout: 60000 });

  const atualIni = await campoInicial.inputValue().catch(() => '');
  const atualFim = await campoFinal.inputValue().catch(() => '');

  if (String(atualIni).trim() === String(startStr).trim() && String(atualFim).trim() === String(endStr).trim()) {
    console.log('   ℹ️ Filtro já está aplicado, pulando reaplicação');
    return;
  }

  await campoInicial.fill('');
  await campoFinal.fill('');
  await campoInicial.fill(startStr);
  await campoFinal.fill(endStr);

  const btnFiltrar = page
    .locator(
      `xpath=//button[contains(., 'Filtrar') or contains(@value, 'Filtrar')] | //input[@type='submit' and contains(@value, 'Filtrar')]`
    )
    .first();

  const clicked = await btnFiltrar.click({ timeout: 60000 }).then(() => true).catch(() => false);
  if (!clicked) {
    await campoFinal.press('Enter').catch(() => {});
  }

  // preferir seletor de resultado (contador) a networkidle, porque a página pode fazer polling
  await page.waitForTimeout(400);
  await page.locator(`xpath=//*[contains(.,'Total de') and contains(.,'registros')]`).first().waitFor({ timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(800);
}

async function fillAndFilter(page, dataInicial, dataFinal) {
  // compat: funções antigas chamam fillAndFilter
  await applyDateFilterIfNeeded(page, dataInicial, dataFinal);
}

function shouldSplitPeriod(totalRegistros) {
  return totalRegistros > SPLIT_THRESHOLD;
}

function splitDateRange(dataInicial, dataFinal) {
  const [d1, m1, y1] = dataInicial.split('/').map(Number);
  const [d2, m2, y2] = dataFinal.split('/').map(Number);

  const start = new Date(y1, m1 - 1, d1);
  const end = new Date(y2, m2 - 1, d2);

  const ranges = [];
  let current = new Date(start);

  while (current < end) {
    const chunkEnd = new Date(current);
    chunkEnd.setDate(chunkEnd.getDate() + SPLIT_DAYS);

    if (chunkEnd > end) {
      ranges.push({
        inicio: current.toLocaleDateString('pt-BR'),
        fim: end.toLocaleDateString('pt-BR'),
      });
    } else {
      ranges.push({
        inicio: current.toLocaleDateString('pt-BR'),
        fim: chunkEnd.toLocaleDateString('pt-BR'),
      });
    }

    current = new Date(chunkEnd);
    current.setDate(current.getDate() + 1);
  }

  return ranges;
}


function brToDate(s) {
  const [d, m, y] = String(s).split('/').map(Number);
  return new Date(y, m - 1, d);
}
function dateToBr(dt) {
  const d = String(dt.getDate()).padStart(2, '0');
  const m = String(dt.getMonth() + 1).padStart(2, '0');
  const y = dt.getFullYear();
  return `${d}/${m}/${y}`;
}

async function processRangeRecursively(page, startStr, endStr, depth, loginType, cert, credencial, pass, reloginCallback) {
  if (depth > MAX_SPLIT_DEPTH) {
    console.log(`   ⚠️ Limite de recursão atingido (depth=${depth}). Prosseguindo sem split: ${startStr}..${endStr}`);
    return await processPeriodInBrowser(page, startStr, endStr, loginType, cert, credencial, pass, reloginCallback, { skipSplitCheck: true });
  }

  await applyDateFilterIfNeeded(page, startStr, endStr);
  updateActivity();

  const total = await getTotalRegistros(page);
  console.log(`   🧩 Subrange atual: ${startStr}..${endStr} (total: ${total})`);

  if (total !== null && total === 0) return { xml: 0, pdf: 0 };

  if (total !== null && total > SPLIT_THRESHOLD) {
    const start = brToDate(startStr);
    const end = brToDate(endStr);

    if (start.getTime() === end.getTime()) {
      console.log(`   ⚠️ Ainda >${SPLIT_THRESHOLD} em 1 dia (${startStr}). Tentando baixar mesmo assim.`);
      return await processPeriodInBrowser(page, startStr, endStr, loginType, cert, credencial, pass, reloginCallback, { skipSplitCheck: true });
    }

    // split binário
    const mid = new Date(start.getTime());
    mid.setDate(mid.getDate() + Math.floor((end.getTime() - start.getTime()) / (1000 * 60 * 60 * 24) / 2));

    const leftStart = startStr;
    const leftEnd = dateToBr(mid);

    const rightStartDate = new Date(mid.getTime());
    rightStartDate.setDate(rightStartDate.getDate() + 1);
    const rightStart = dateToBr(rightStartDate);
    const rightEnd = endStr;

    const left = await processRangeRecursively(page, leftStart, leftEnd, depth + 1, loginType, cert, credencial, pass, reloginCallback);
    const right = await processRangeRecursively(page, rightStart, rightEnd, depth + 1, loginType, cert, credencial, pass, reloginCallback);

    return { xml: left.xml + right.xml, pdf: left.pdf + right.pdf };
  }

  // total <= threshold (ou total=null): baixa normal
  return await processPeriodInBrowser(page, startStr, endStr, loginType, cert, credencial, pass, reloginCallback, { skipSplitCheck: true });
}
const ITEMS_PER_PAGE = 15;

async function getTotalRegistros(page) {
  try {
    const totalTxt = await page.locator(`xpath=//*[contains(.,'Total de') and contains(.,'registros')]`).first().innerText().catch(() => '');
    const mTotal = /Total\s+de\s+(\d+)\s+registros/i.exec(totalTxt || '');
    if (mTotal) {
      return parseInt(mTotal[1], 10);
    }
  } catch (e) {
    // Ignora erros
  }
  return null;
}

let downloadDir = '';
let reloginUsed = false;
let totalDownloadsDone = 0;

// Configuração de ritmo de download (anti-detecção) - OTIMIZADA
// Padrão: rápido -> pausa 1min -> rápido -> pausa 1min...
const DOWNLOAD_PATTERN = {
  FAST_COUNT: 100,        // Downloads rápidos por ciclo (até 100)
  FAST_DELAY_MIN: 50,     // Mínimo ms entre downloads na fase rápida
  FAST_DELAY_MAX: 150,    // Máximo ms entre downloads na fase rápida
  PAUSE_EVERY: 100,       // Pausa a cada X downloads
  PAUSE_MIN_MS: 60000,    // 1 minuto de pausa
  PAUSE_MAX_MS: 70000,    // 70 segundos (margem)
  PAGE_PAUSE_MIN: 2000,  // Pausa mínima entre páginas (2s)
  PAGE_PAUSE_MAX: 4000,  // Pausa máxima entre páginas (4s)
};

// Pausa aleatória entre ações para imitar humano
async function randomHumanPause(minMs = 500, maxMs = 1500) {
  const delay = Math.floor(Math.random() * (maxMs - minMs)) + minMs;
  await new Promise(r => setTimeout(r, delay));
}

function getPauseForPosition(positionInCycle) {
  // Calcula posição no ciclo (a cada 100 downloads)
  const cyclePosition = positionInCycle % DOWNLOAD_PATTERN.PAUSE_EVERY;
  const currentBatch = Math.floor(positionInCycle / DOWNLOAD_PATTERN.PAUSE_EVERY);
  
  // Se completou 100 downloads (cyclePosition volta a 0), fazer pausa
  if (cyclePosition === 0 && positionInCycle > 0) {
    // Pausa de 1 minuto entre ciclos de 100
    const pauseMs = Math.floor(DOWNLOAD_PATTERN.PAUSE_MIN_MS + Math.random() * (DOWNLOAD_PATTERN.PAUSE_MAX_MS - DOWNLOAD_PATTERN.PAUSE_MIN_MS));
    return {
      ms: pauseMs,
      phase: '⏸️ PAUSA 1MIN',
      isPause: true,
      batch: currentBatch
    };
  }
  
  // Fase rápida: delays pequenos
  return {
    ms: Math.floor(DOWNLOAD_PATTERN.FAST_DELAY_MIN + Math.random() * (DOWNLOAD_PATTERN.FAST_DELAY_MAX - DOWNLOAD_PATTERN.FAST_DELAY_MIN)),
    phase: '⚡ RÁPIDO',
    isPause: false,
    batch: currentBatch
  };
}

async function maybePauseAfterDownloads() {
  if (totalDownloadsDone > 0) {
    const pause = getPauseForPosition(totalDownloadsDone);
    
    // Log para acompanhamento
    if (pause.isPause) {
      console.log(`\n   ⏸️══════════════════════════════════════════`);
      console.log(`   ⏸️ PAUSA DE 1 MINUTO APÓS ${pause.batch * DOWNLOAD_PATTERN.PAUSE_EVERY} DOWNLOADS`);
      console.log(`   ⏸️══════════════════════════════════════════\n`);
    }
    
    await new Promise((r) => setTimeout(r, pause.ms));
    
    // Log de continuação após pausa
    if (pause.isPause) {
      console.log(`   ▶️ Continuando downloads (lote ${pause.batch + 1})...\n`);
    }
  }
}


async function processPeriodInBrowser(page, dataInicial, dataFinal, loginType, cert, credencial, pass, reloginCallback, opts = {}) {
  let totalXml = 0;
  let totalPdf = 0;

  // Aplicar filtro (sem recarregar) — só reaplica se necessário
  await applyDateFilterIfNeeded(page, dataInicial, dataFinal);
  updateActivity();

  const totalRegistros = await getTotalRegistros(page);
  console.log(`   📊 Período: ${dataInicial} a ${dataFinal}`);
  console.log(`   📊 Total de registros: ${totalRegistros}`);
  updateActivity();

  if (totalRegistros !== null && totalRegistros === 0) {
    console.log(`   ℹ️ 0 notas no período.`);
    return { xml: 0, pdf: 0, needToSplit: false };
  }

  // Se excedeu o limite, faz split interno recursivo (mesma sessão / mesma página)
  if (!opts.skipSplitCheck && totalRegistros !== null && shouldSplitPeriod(totalRegistros)) {
    console.log(`   ⚠️ Período grande (> ${SPLIT_THRESHOLD}). Processando por subranges no mesmo navegador: ${dataInicial}..${dataFinal}`);

    const res = await processRangeRecursively(page, dataInicial, dataFinal, 0, loginType, cert, credencial, pass, reloginCallback);
    return { xml: res.xml, pdf: res.pdf, needToSplit: false };
  }

  let totalPages = 1;
  if (totalRegistros !== null && totalRegistros > ITEMS_PER_PAGE) {
    totalPages = Math.ceil(totalRegistros / ITEMS_PER_PAGE);
    console.log(`   📊 Páginas: ${totalPages}`);
  } else if (totalRegistros !== null && totalRegistros <= ITEMS_PER_PAGE) {
    console.log(`   📊 Baixando somente a primeira página.`);
  }

  if (totalRegistros !== null && totalRegistros <= ITEMS_PER_PAGE) {
    const result = await downloadPageItems(page, downloadDir, 0);
    return { xml: result.xml, pdf: result.pdf, needToSplit: false };
  }

  let reloginAttempts = 0;

  for (let pagina = 1; pagina <= totalPages; pagina++) {
    if (await isSessionExpired(page)) {
      console.log('   ⚠️ Sessão expirada!');

      if (reloginAttempts < MAX_RELOGIN_ATTEMPTS) {
        reloginAttempts++;
        await reloginCallback(dataInicial, dataFinal);
        continue;
      } else {
        throw new Error('Máximo de tentativas de re-login atingido');
      }
    }

    const bodyText = (await page.locator('body').innerText().catch(() => '')).toLowerCase();
    const msgs = ['nenhum registro', 'nenhum resultado', 'não há dados', 'sem resultados', 'nenhuma nota'];
    if (msgs.some((m) => bodyText.includes(m))) {
      console.log(`   ℹ️ Página ${pagina}: sem registros.`);
      break;
    }

    console.log(`   📄 Processando página ${pagina}/${totalPages}`);

    const xmlsThisPage = await downloadPageItems(page, downloadDir, pagina);
    totalXml += xmlsThisPage.xml;
    totalPdf += xmlsThisPage.pdf;

    saveCheckpoint(downloadDir, {
      alias: cert.alias,
      dataInicial: dataInicial,
      dataFinal: dataFinal,
      pagina: pagina,
      totalXml,
      totalPdf,
      timestamp: Date.now(),
    });

    if (pagina < totalPages) {
      if (await isSessionExpired(page)) {
        console.log('   ⚠️ Sessão expirada antes de clicar próxima!');

        if (reloginAttempts < MAX_RELOGIN_ATTEMPTS) {
          reloginAttempts++;
          await reloginCallback(dataInicial, dataFinal);
          continue;
        } else {
          throw new Error('Máximo de tentativas de re-login atingido');
        }
      }

      const nextLi = page.locator(`xpath=//li[a/i[contains(@class,'fa-angle-right')]]`).first();
      const lastLi = page.locator(`xpath=//li[a/i[contains(@class,'fa-angle-double-right')]]`).first();

      const nextLiClass = ((await nextLi.getAttribute('class').catch(() => '')) || '').toLowerCase();
      const lastLiClass = ((await lastLi.getAttribute('class').catch(() => '')) || '').toLowerCase();

      if (nextLiClass.includes('disabled') && lastLiClass.includes('disabled')) {
        break;
      }

      if (nextLiClass.includes('disabled')) {
        break;
      }

      const nextA = nextLi.locator('xpath=.//a').first();
      await nextA.scrollIntoViewIfNeeded().catch(() => {});
      
      // Pausa humana aleatória antes de clicar na próxima página
      await randomHumanPause(DOWNLOAD_PATTERN.PAGE_PAUSE_MIN, DOWNLOAD_PATTERN.PAGE_PAUSE_MAX);
      
      await nextA.click({ timeout: 60000 }).catch(() => {});
      await safeWaitNetworkIdle(page, 60000);
      await page.waitForTimeout(1200);
      updateActivity();
    }
  }

  return { xml: totalXml, pdf: totalPdf, needToSplit: false };
}

async function loginAndDownload({ 
  cert, 
  credencial, 
  dataInicial, 
  dataFinal, 
  downloadDir: dd, 
  headless,
  loginType 
}) {
  downloadDir = dd;
  
  let pass = null;
  if (loginType === 'certificado') {
    pass = process.env.PFX_PASS || (cert.passEnv ? process.env[cert.passEnv] : null);
    if (!pass) {
      throw new Error(`Senha não encontrada. Defina PFX_PASS.`);
    }
    if (!cert.pfxPath) throw new Error(`Certificado '${cert.alias}' sem pfxPath no certs.json.`);
  } else {
    pass = process.env.LOGIN_PASS;
    if (!pass) {
      throw new Error(`Senha do portal não encontrada. Defina LOGIN_PASS.`);
    }
    if (!credencial || !credencial.cpf_cnpj) {
      throw new Error(`Credencial '${cert.alias}' sem CPF/CNPJ.`);
    }
  }

  ensureDir(downloadDir);

  // Limpa checkpoint antigo se for novo período
  const checkpoint = loadCheckpoint(downloadDir);
  if (!checkpoint || checkpoint.dataInicial !== dataInicial || checkpoint.dataFinal !== dataFinal) {
    clearCheckpoint(downloadDir);
  }

  let browser = null;
  let context = null;
  let page = null;

  try {
    browser = await chromium.launch({
      headless: String(headless).toLowerCase() === 'true',
      slowMo: 0,
      args: [
        '--disable-web-security',
        '--ignore-certificate-errors',
        '--allow-running-insecure-content',
        '--disable-setuid-sandbox',
        '--no-sandbox',
      ],
    });

    if (loginType === 'certificado') {
      const pfxAbs = path.resolve(cert.pfxPath);
      if (!fs.existsSync(pfxAbs)) throw new Error(`PFX não encontrado em: ${pfxAbs}`);

      context = await browser.newContext({
        acceptDownloads: true,
        downloadsPath: downloadDir,
        clientCertificates: CERT_ORIGINS.map((origin) => ({
          origin,
          pfxPath: pfxAbs,
          passphrase: pass,
        })),
      });
    } else {
      context = await browser.newContext({
        acceptDownloads: true,
        downloadsPath: downloadDir,
      });
    }

    page = await context.newPage();

    startKeepAlive(page);
    updateActivity();

    // Função de re-login
    const reloginCallback = async (di, df) => {
      if (loginType === 'certificado') {
        await reloginWithCert(page, cert, di, df);
      } else {
        await reloginWithCredentials(page, credencial, pass, di, df);
      }
    };

    // Inicia o timer de re-login a cada 2 minutos
    startReloginTimer(page, () => reloginCallback(dataInicial, dataFinal));

    // Login
    if (loginType === 'certificado') {
      console.log('   🔐 Fazendo login com certificado digital...');
      
      await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 120000 });
      await safeWaitNetworkIdle(page, 60000);

      const certLink = page.locator('a[href="/EmissorNacional/Certificado"]').first();
      await certLink.waitFor({ state: 'attached', timeout: 60000 });
      await certLink.scrollIntoViewIfNeeded().catch(() => {});
      await Promise.allSettled([
        page.waitForNavigation({ timeout: 120000, waitUntil: 'domcontentloaded' }),
        certLink.click({ timeout: 60000, force: true }),
      ]);
      await safeWaitNetworkIdle(page, 60000);

      assertNotOnLogin(page.url());

      const notasLink = page.locator(`a[href="${NOTAS_URL}"]`).first();
      const logou = await notasLink.isVisible().catch(() => false);
      if (!logou) {
        const shot = path.join(downloadDir, `nao_logado_${Date.now()}.png`);
        await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
        throw new Error(`Login não confirmado.`);
      }

      await Promise.allSettled([
        page.waitForNavigation({ timeout: 120000, waitUntil: 'domcontentloaded' }),
        notasLink.click({ timeout: 60000 }),
      ]);
    } else {
      await loginWithCredentials(page, credencial.cpf_cnpj, pass);
      
      const notasLink = page.locator(`a[href="${NOTAS_URL}"]`).first();
      await Promise.allSettled([
        page.waitForNavigation({ timeout: 120000, waitUntil: 'domcontentloaded' }),
        notasLink.click({ timeout: 60000 }),
      ]);
    }

    await safeWaitNetworkIdle(page, 60000);
    assertNotOnLogin(page.url());
    updateActivity();

    // Processa o período (com divisão automática se > 800 registros)
    const result = await processPeriodInBrowser(
      page, 
      dataInicial, 
      dataFinal, 
      loginType, 
      cert, 
      credencial, 
      pass, 
      reloginCallback
    );

    clearCheckpoint(downloadDir);
    console.log(`   ✅ Download concluído: XMLs=${result.xml}, PDFs=${result.pdf}`);
    return { 
      ok: true, 
      alias: cert.alias, 
      cnpj: credencial ? credencial.cpf_cnpj : null, 
      totalXml: result.xml, 
      totalPdf: result.pdf, 
      downloadDir, 
      needToSplit: false 
    };
  } catch (error) {
    console.log(`   ❌ Erro no download: ${error.message}`);
    
    // Detectar erros de rede específicos
    const errMsg = String(error.message || '').toLowerCase();
    if (errMsg.includes('econnreset') || errMsg.includes('connection reset')) {
      throw new Error('Erro de conexão: O servidor resetou a conexão. Possíveis causas: certificado inválido, SSL/TLS incompatível, ou firewall/proxy bloqueando. Tente novamente ou verifique a conexão.');
    }
    if (errMsg.includes('certificado') || errMsg.includes('certificate')) {
      throw new Error(`Erro de certificado: ${error.message}`);
    }
    
    throw error;
  } finally {
    stopKeepAlive();
    stopReloginTimer();
    if (page) await page.close().catch(() => {});
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
  }
}

async function downloadPageItems(page, downloadDir, pagina) {
  let xmlCount = 0;
  let pdfCount = 0;

  const menus = page.locator(
    `xpath=//a[contains(@class, 'icone-trigger')] | //button[.//i[contains(@class, 'glyphicon-option-vertical')]] | //a[.//i[contains(@class, 'glyphicon-option-vertical')]]`
  );

  const count = await menus.count();
  if (count === 0) {
    console.log(`   ℹ️ Página ${pagina}: nenhum item.`);
    return { xml: 0, pdf: 0 };
  }

  console.log(`   📥 Página ${pagina}: ${count} itens`);

  for (let i = 0; i < count; i++) {
    if (await isSessionExpired(page)) {
      console.log('   ⚠️ Sessão expirada durante download!');
      break;
    }

    const menu = menus.nth(i);
    try {
      await menu.scrollIntoViewIfNeeded().catch(() => {});
      await menu.click({ timeout: 20000 });
      await page.waitForTimeout(250);
      updateActivity();

      const row = menu.locator('xpath=ancestor::tr[1]');
      const inRow = (await row.count()) > 0;
      const scope = inRow ? row : page;

      const linkXml = scope.locator(
        `xpath=.//a[contains(., 'Download XML') or contains(., 'download XML') or (contains(., 'XML') and not(contains(., 'DANFS')))]`
      );
      const savedXml = await clickAndSaveDownload(page, linkXml, downloadDir).catch(() => null);
      if (savedXml) { xmlCount += 1; totalDownloadsDone += 1; await maybePauseAfterDownloads(); }

      await page.waitForTimeout(150);

      const linkPdfProbe = scope
        .locator(
          `xpath=.//a[contains(., 'Download DANFS-e') or contains(., 'DANFS-e') or contains(., 'DANFSe') or contains(., 'PDF')]`
        )
        .first();

      if (!(await linkPdfProbe.isVisible().catch(() => false))) {
        await menu.click({ timeout: 20000 }).catch(() => {});
        await page.waitForTimeout(200);
      }

      const linkPdf = scope.locator(
        `xpath=.//a[contains(., 'Download DANFS-e') or contains(., 'DANFS-e') or contains(., 'DANFSe') or contains(., 'PDF')]`
      );
      const savedPdf = await clickAndSaveDownload(page, linkPdf, downloadDir).catch(() => null);
      if (savedPdf) { pdfCount += 1; totalDownloadsDone += 1; await maybePauseAfterDownloads(); }
    } catch (_) {
      // ignora erros por item
    } finally {
      await page.keyboard.press('Escape').catch(() => {});
      await page.waitForTimeout(150);
    }
  }

  console.log(`   ✅ Página ${pagina}: XMLs=${xmlCount}, PDFs=${pdfCount}`);
  return { xml: xmlCount, pdf: pdfCount };
}

(async () => {
  const args = parseArgs(process.argv);

  const alias = pickArg(args, ['alias', 'certAlias', 'cert_alias']);
  const dataInicial = pickArg(args, ['dataInicial', 'data_inicial']);
  const dataFinal = pickArg(args, ['dataFinal', 'data_final']);
  const downloadDir = pickArg(args, ['downloadDir', 'download_dir', 'diretorio_download', 'tmpDir', 'tmp_dir']);
  const headless = pickArg(args, ['headless']) || 'false';
  const certsJsonPath = pickArg(args, ['certsJson', 'certs_json_path', 'certs_json']);
  const credentialsJsonPath = pickArg(args, ['credentialsJson', 'credentials_json_path', 'credentials_json']);
  const loginType = pickArg(args, ['loginType', 'login_type']) || 'certificado';
  const tipoNota = pickArg(args, ['tipoNota', 'tipo_nota', 'tipo']) || 'tomados';  // 'tomados' ou 'prestados'

  // Configura a URL correta com base no tipo de nota
  if (tipoNota === 'prestados') {
    NOTAS_URL = '/EmissorNacional/Notas/Emitidas';
    NOTAS_URL_CHECK = '/notas/emitidas';
    console.log('   📋 Tipo de nota: Prestados (Emitidas)');
  } else {
    NOTAS_URL = '/EmissorNacional/Notas/Recebidas';
    NOTAS_URL_CHECK = '/notas/recebidas';
    console.log('   📋 Tipo de nota: Tomados (Recebidas)');
  }

  if (!alias || !dataInicial || !dataFinal || !downloadDir) {
    throw new Error(
      'Argumentos ausentes. Necessário: --alias, --dataInicial, --dataFinal, --downloadDir.'
    );
  }

  let cert = null;
  let credencial = null;

  if (loginType === 'cpf_cnpj') {
    const credenciais = readCredentials(credentialsJsonPath);
    credencial = credenciais.find((c) => String(c.alias).toLowerCase() === String(alias).toLowerCase());
    if (!credencial) {
      throw new Error(`Credencial não encontrada para alias '${alias}'.`);
    }
    cert = { alias: alias };
  } else {
    const certs = readCerts(certsJsonPath);
    cert = certs.find((c) => String(c.alias).toLowerCase() === String(alias).toLowerCase());
    if (!cert) {
      throw new Error(`Certificado não encontrado para alias '${alias}'.`);
    }
  }

  const result = await loginAndDownload({ 
    cert, 
    credencial, 
    dataInicial, 
    dataFinal, 
    downloadDir, 
    headless,
    loginType 
  });
  process.stdout.write(JSON.stringify(result));
})().catch((err) => {
  const payload = {
    ok: false,
    error: String(err && err.message ? err.message : err),
    stack: err && err.stack ? String(err.stack) : null,
  };
  console.error(payload.error);
  process.stdout.write(JSON.stringify(payload));
  process.exit(1);
});
