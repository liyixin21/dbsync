// ============================================================
// DBSync - Frontend Application (@material/web)
// ============================================================

// ============================================================
// Import: only necessary components
// ============================================================
import '@material/web/button/filled-button.js';
import '@material/web/button/outlined-button.js';
import '@material/web/button/text-button.js';
import '@material/web/iconbutton/icon-button.js';
import '@material/web/textfield/outlined-text-field.js';
import '@material/web/select/filled-select.js';
import '@material/web/select/select-option.js';

// ============================================================
// Import official MD3 color utilities
// ============================================================
import {
  Hct,
  argbFromHex,
  hexFromArgb,
  SchemeTonalSpot,
  SchemeContent,
  SchemeFidelity,
  SchemeMonochrome,
  SchemeNeutral,
  SchemeVibrant,
  SchemeExpressive,
  SchemeFruitSalad,
  SchemeRainbow,
} from '@material/material-color-utilities';

// ============================================================
// API Helper
// ============================================================
const API_BASE = '/api';

async function api(path, options = {}) {
  const url = `${API_BASE}${path}`;
  const config = { headers: { 'Content-Type': 'application/json' }, ...options };
  try {
    const res = await fetch(url, config);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
    return data;
  } catch (e) {
    showToast(e.message, 'error');
    throw e;
  }
}

// ============================================================
// Toast Notifications
// ============================================================
function showToast(message, type = 'info') {
  // Simple alert fallback for now
  alert(message);
}
window.showToast = showToast;

// ============================================================
// Theme Engine (using @material/material-color-utilities)
// ============================================================
const schemeConstructors = {
  TonalSpot: SchemeTonalSpot,
  Content: SchemeContent,
  Fidelity: SchemeFidelity,
  Monochrome: SchemeMonochrome,
  Neutral: SchemeNeutral,
  Vibrant: SchemeVibrant,
  Expressive: SchemeExpressive,
  FruitSalad: SchemeFruitSalad,
  Rainbow: SchemeRainbow,
};

const TOKEN_NAMES = [
  'primary','on-primary','primary-container','on-primary-container',
  'secondary','on-secondary','secondary-container','on-secondary-container',
  'tertiary','on-tertiary','tertiary-container','on-tertiary-container',
  'error','on-error','error-container','on-error-container',
  'surface','on-surface','surface-variant','on-surface-variant',
  'outline','outline-variant',
  'inverse-surface','inverse-on-surface','inverse-primary',
  'surface-dim','surface-bright',
  'surface-container-lowest','surface-container-low',
  'surface-container','surface-container-high','surface-container-highest',
];

let isDark = false;

function generateScheme(hex, schemeName, contrastLevel) {
  const argb = argbFromHex(hex);
  const hct = Hct.fromInt(argb);
  const Ctor = schemeConstructors[schemeName] || SchemeTonalSpot;
  const light = new Ctor(hct, false, parseFloat(contrastLevel));
  const dark = new Ctor(hct, true, parseFloat(contrastLevel));
  return { light, dark };
}

function schemeToMap(scheme) {
  const map = {};
  for (const name of TOKEN_NAMES) {
    const camel = name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    let val = scheme[camel];
    if (val === undefined) {
      const alt = name.split('-').map((s, i) => i === 0 ? s : s[0].toUpperCase() + s.slice(1)).join('');
      val = scheme[alt];
    }
    map[name] = val !== undefined ? hexFromArgb(val) : '--';
  }
  return map;
}

function applyTheme(lightMap, darkMap) {
  const root = document.documentElement;
  for (const [name, hex] of Object.entries(lightMap)) {
    root.style.setProperty(`--md-sys-color-${name}`, hex);
  }
  root.dataset.darkColors = JSON.stringify(darkMap);
  if (isDark) {
    for (const [name, hex] of Object.entries(darkMap)) {
      root.style.setProperty(`--md-sys-color-${name}`, hex);
    }
  }
  renderTokens(lightMap, darkMap);
}

function renderTokens(lightMap, darkMap) {
  const grid = document.getElementById('tokensGrid');
  if (!grid) return;
  const colors = isDark ? darkMap : lightMap;
  grid.innerHTML = TOKEN_NAMES.map(name => {
    const hex = colors[name] || '#888888';
    return `
      <div class="token-card">
        <div class="token-swatch" style="background:${hex}"></div>
        <div class="token-info">
          <div class="token-name">${name}</div>
          <div class="token-hex">${hex}</div>
        </div>
      </div>
    `;
  }).join('');
}

function updateTheme() {
  const hex = document.getElementById('colorPicker').value;
  const contrast = document.getElementById('contrastSelect').value;
  document.getElementById('sourceHex').textContent = hex.toUpperCase();
  const { light, dark } = generateScheme(hex, 'TonalSpot', contrast);
  applyTheme(schemeToMap(light), schemeToMap(dark));
  // Save theme
  api('/system/theme', { method: 'PUT', body: JSON.stringify({ primary_color: hex, dark_mode: isDark }) }).catch(() => {});
}

// ============================================================
// Dark mode toggle
// ============================================================
const themeToggle = document.getElementById('themeToggle');

function toggleTheme() {
  isDark = !isDark;
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  themeToggle.querySelector('span').textContent = isDark ? 'light_mode' : 'dark_mode';
  updateTheme();
}

themeToggle.addEventListener('click', toggleTheme);

// ============================================================
// Event listeners
// ============================================================
document.getElementById('colorPicker').addEventListener('input', updateTheme);
document.getElementById('contrastSelect').addEventListener('change', updateTheme);

// ============================================================
// Navigation
// ============================================================
const pageTitles = { dashboard: '仪表板', databases: '数据库管理', sync: '同步管理', backup: '备份管理', settings: '设置' };

function showPage(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item[data-page]').forEach(n => n.classList.remove('active'));
  document.getElementById(`page-${page}`)?.classList.add('active');
  document.querySelector(`.nav-item[data-page="${page}"]`)?.classList.add('active');
  document.getElementById('pageTitle').textContent = pageTitles[page] || page;
  // Load page data
  if (page === 'dashboard') loadDashboard();
  else if (page === 'databases') loadDatabases();
  else if (page === 'sync') loadSyncTasks();
  else if (page === 'backup') { loadBackupPlans(); loadBackupHistory(); }
}
window.showPage = showPage;

// Toggle drawer
document.getElementById('menubtn').addEventListener('click', () => {
  document.getElementById('drawer').classList.toggle('collapsed');
});

// ============================================================
// Dialogs
// ============================================================
function openDialog(dialog) {
  dialog.showModal();
  document.body.classList.add('dialog-open');
}
function closeWithAnim(dialog) {
  dialog.classList.add('closing');
  setTimeout(() => {
    dialog.close();
    dialog.classList.remove('closing');
    document.body.classList.remove('dialog-open');
  }, 200);
}

// Database Dialog
const databaseDialog = document.getElementById('databaseDialog');
document.getElementById('databaseDialogCancelBtn').addEventListener('click', () => closeWithAnim(databaseDialog));
databaseDialog.addEventListener('click', (e) => { if (e.target === databaseDialog) closeWithAnim(databaseDialog); });

// Sync Task Dialog
const syncTaskDialog = document.getElementById('syncTaskDialog');
document.getElementById('syncTaskDialogCancelBtn').addEventListener('click', () => closeWithAnim(syncTaskDialog));
syncTaskDialog.addEventListener('click', (e) => { if (e.target === syncTaskDialog) closeWithAnim(syncTaskDialog); });

// Backup Plan Dialog
const backupPlanDialog = document.getElementById('backupPlanDialog');
document.getElementById('backupPlanDialogCancelBtn').addEventListener('click', () => closeWithAnim(backupPlanDialog));
backupPlanDialog.addEventListener('click', (e) => { if (e.target === backupPlanDialog) closeWithAnim(backupPlanDialog); });

// Confirm Dialog
const confirmDialog = document.getElementById('confirmDialog');
let confirmCallback = null;
document.getElementById('confirmDialogCancelBtn').addEventListener('click', () => {
  closeWithAnim(confirmDialog);
  confirmCallback = null;
});
document.getElementById('confirmDialogOkBtn').addEventListener('click', () => {
  closeWithAnim(confirmDialog);
  if (confirmCallback) confirmCallback();
  confirmCallback = null;
});
confirmDialog.addEventListener('click', (e) => {
  if (e.target === confirmDialog) closeWithAnim(confirmDialog);
});

function showConfirm(title, message, onConfirm) {
  document.getElementById('confirmDialogTitle').textContent = title;
  document.getElementById('confirmDialogContent').textContent = message;
  confirmCallback = onConfirm;
  openDialog(confirmDialog);
}
window.showConfirm = showConfirm;

// ============================================================
// Dashboard
// ============================================================
async function loadDashboard() {
  try {
    const [dbList, syncList, planList] = await Promise.all([
      api('/databases/'),
      api('/sync-tasks/'),
      api('/backup-plans/')
    ]);
    document.getElementById('databasesCount').textContent = dbList.length;
    document.getElementById('syncTasksCount').textContent = syncList.filter(t => t.status === 'running').length;
    document.getElementById('backupPlansCount').textContent = planList.filter(p => p.is_active).length;
    // Load recent backups
    try {
      const history = await api('/backup-history/?limit=5');
      const tbody = document.getElementById('recentBackups');
      if (history.items && history.items.length > 0) {
        tbody.innerHTML = history.items.map(h => `<tr>
          <td>${formatTime(h.created_at)}</td>
          <td>计划 #${h.backup_plan_id}</td>
          <td>${h.backup_type === 'full' ? '全量' : '增量'}</td>
          <td><span class="status-badge status-${h.status}">${statusText(h.status)}</span></td>
          <td>${formatSize(h.file_size)}</td>
        </tr>`).join('');
      }
      // Load total size
      const stats = await api('/backup-history/statistics');
      document.getElementById('backupSize').textContent = formatSize(stats.total_size);
    } catch (e) { /* ignore */ }
  } catch (e) { console.error('Dashboard load failed:', e); }
}

// ============================================================
// Databases
// ============================================================
let allDatabases = [];

async function loadDatabases() {
  try {
    allDatabases = await api('/databases/');
    const tbody = document.getElementById('databasesList');
    if (allDatabases.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无数据库配置，点击"添加数据库"开始</p></td></tr>';
      return;
    }
    tbody.innerHTML = allDatabases.map(db => `<tr>
      <td><strong>${esc(db.name)}</strong></td>
      <td>${esc(db.host)}</td>
      <td>${db.port}</td>
      <td>${esc(db.database_name)}</td>
      <td><span class="status-badge ${db.is_active ? 'status-running' : 'status-stopped'}">${db.is_active ? '启用' : '禁用'}</span></td>
      <td class="actions-row">
        <md-icon-button onclick="editDatabase(${db.id})"><span class="material-symbols-outlined">edit</span></md-icon-button>
        <md-icon-button onclick="deleteDatabase(${db.id},'${esc(db.name)}')"><span class="material-symbols-outlined">delete</span></md-icon-button>
      </td>
    </tr>`).join('');
  } catch (e) { console.error('Load databases failed:', e); }
}

function openDatabaseDialog(db = null) {
  document.getElementById('databaseDialogTitle').textContent = db ? '编辑数据库' : '添加数据库';
  document.getElementById('dbId').value = db ? db.id : '';
  document.getElementById('dbName').value = db ? db.name : '';
  document.getElementById('dbHost').value = db ? db.host : '';
  document.getElementById('dbPort').value = db ? db.port : 3306;
  document.getElementById('dbUsername').value = db ? db.username : '';
  document.getElementById('dbPassword').value = '';
  document.getElementById('dbDatabaseName').value = db ? db.database_name : '';
  openDialog(databaseDialog);
}
window.openDatabaseDialog = openDatabaseDialog;

window.editDatabase = async function(id) {
  const db = allDatabases.find(d => d.id === id);
  if (db) openDatabaseDialog(db);
};

document.getElementById('saveDatabaseBtn').addEventListener('click', async () => {
  const id = document.getElementById('dbId').value;
  const data = {
    name: document.getElementById('dbName').value,
    host: document.getElementById('dbHost').value,
    port: parseInt(document.getElementById('dbPort').value),
    username: document.getElementById('dbUsername').value,
    password: document.getElementById('dbPassword').value,
    database_name: document.getElementById('dbDatabaseName').value
  };
  if (!data.name || !data.host || !data.username || !data.database_name) { showToast('请填写所有必填字段', 'error'); return; }
  try {
    if (id) {
      if (!data.password) delete data.password;
      await api(`/databases/${id}`, { method: 'PUT', body: JSON.stringify(data) });
      showToast('数据库更新成功', 'success');
    } else {
      if (!data.password) { showToast('请输入密码', 'error'); return; }
      await api('/databases/', { method: 'POST', body: JSON.stringify(data) });
      showToast('数据库添加成功', 'success');
    }
    closeWithAnim(databaseDialog);
    loadDatabases();
  } catch (e) {}
});

document.getElementById('testConnectionBtn').addEventListener('click', async () => {
  const data = {
    host: document.getElementById('dbHost').value,
    port: parseInt(document.getElementById('dbPort').value),
    username: document.getElementById('dbUsername').value,
    password: document.getElementById('dbPassword').value,
    database_name: document.getElementById('dbDatabaseName').value
  };
  try {
    const res = await api('/databases/test-connection', { method: 'POST', body: JSON.stringify(data) });
    showToast(res.message, res.success ? 'success' : 'error');
  } catch (e) {}
});

window.deleteDatabase = async function(id, name) {
  showConfirm('删除数据库', `确定要删除数据库 "${name}" 吗？`, async () => {
    try {
      await api(`/databases/${id}`, { method: 'DELETE' });
      showToast('删除成功', 'success');
      loadDatabases();
    } catch (e) {}
  });
};

// ============================================================
// Sync Tasks
// ============================================================
async function loadSyncTasks() {
  try {
    const tasks = await api('/sync-tasks/');
    if (tasks.length === 0) {
      document.getElementById('syncTasksList').innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无同步任务</p></td></tr>';
      return;
    }
    // Load db names
    if (allDatabases.length === 0) await loadDatabases();
    const dbMap = Object.fromEntries(allDatabases.map(d => [d.id, d.name]));
    document.getElementById('syncTasksList').innerHTML = tasks.map(t => `<tr>
      <td><strong>${esc(t.name)}</strong></td>
      <td>${esc(dbMap[t.source_db_id] || '?')}</td>
      <td>${esc(dbMap[t.target_db_id] || '?')}</td>
      <td><span class="status-badge status-${t.status}">${statusText(t.status)}</span></td>
      <td>${t.last_sync_time ? formatTime(t.last_sync_time) : '-'}</td>
      <td class="actions-row">
        ${t.status === 'running'
          ? `<md-icon-button onclick="stopSyncTask(${t.id})"><span class="material-symbols-outlined">stop</span></md-icon-button>`
          : `<md-icon-button onclick="startSyncTask(${t.id})"><span class="material-symbols-outlined">play_arrow</span></md-icon-button>`}
        <md-icon-button onclick="deleteSyncTask(${t.id},'${esc(t.name)}')"><span class="material-symbols-outlined">delete</span></md-icon-button>
      </td>
    </tr>`).join('');
  } catch (e) { console.error('Load sync tasks failed:', e); }
}

function openSyncTaskDialog() {
  if (allDatabases.length === 0) loadDatabases();
  if (allDatabases.length < 2) { showToast('请先添加至少两个数据库', 'error'); return; }
  // Populate db selects
  const src = document.getElementById('syncSource');
  const tgt = document.getElementById('syncTarget');
  src.innerHTML = allDatabases.map(d => `<md-select-option value="${d.id}"><div slot="headline">${esc(d.name)}</div></md-select-option>`).join('');
  tgt.innerHTML = allDatabases.map(d => `<md-select-option value="${d.id}"><div slot="headline">${esc(d.name)}</div></md-select-option>`).join('');
  src.value = String(allDatabases[0].id);
  tgt.value = String(allDatabases[1].id);
  document.getElementById('syncName').value = '';
  openDialog(syncTaskDialog);
}
window.openSyncTaskDialog = openSyncTaskDialog;

document.getElementById('saveSyncTaskBtn').addEventListener('click', async () => {
  const data = {
    name: document.getElementById('syncName').value,
    source_db_id: parseInt(document.getElementById('syncSource').value),
    target_db_id: parseInt(document.getElementById('syncTarget').value)
  };
  if (!data.name) { showToast('请输入任务名称', 'error'); return; }
  if (data.source_db_id === data.target_db_id) { showToast('源数据库和目标数据库不能相同', 'error'); return; }
  try {
    await api('/sync-tasks/', { method: 'POST', body: JSON.stringify(data) });
    showToast('同步任务创建成功', 'success');
    closeWithAnim(syncTaskDialog);
    loadSyncTasks();
  } catch (e) {}
});

window.startSyncTask = async function(id) {
  try { await api(`/sync-tasks/${id}/start`, { method: 'POST' }); showToast('任务启动成功', 'success'); loadSyncTasks(); } catch (e) {}
};
window.stopSyncTask = async function(id) {
  try { await api(`/sync-tasks/${id}/stop`, { method: 'POST' }); showToast('任务已停止', 'success'); loadSyncTasks(); } catch (e) {}
};
window.deleteSyncTask = async function(id, name) {
  showConfirm('删除同步任务', `确定要删除任务 "${name}" 吗？`, async () => {
    try { await api(`/sync-tasks/${id}`, { method: 'DELETE' }); showToast('删除成功', 'success'); loadSyncTasks(); } catch (e) {}
  });
};

// ============================================================
// Backup Plans
// ============================================================
async function loadBackupPlans() {
  try {
    const plans = await api('/backup-plans/');
    if (allDatabases.length === 0) await loadDatabases();
    const dbMap = Object.fromEntries(allDatabases.map(d => [d.id, d.name]));
    if (plans.length === 0) {
      document.getElementById('backupPlansList').innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无备份计划</p></td></tr>';
      return;
    }
    document.getElementById('backupPlansList').innerHTML = plans.map(p => `<tr>
      <td><strong>${esc(p.name)}</strong></td>
      <td>${esc(dbMap[p.database_id] || '?')}</td>
      <td>${p.backup_type === 'full' ? '全量' : '增量'}</td>
      <td>${p.schedule_interval ? `每 ${p.schedule_interval} 分钟` : p.schedule_cron || '-'}</td>
      <td><span class="status-badge ${p.is_active ? 'status-running' : 'status-stopped'}">${p.is_active ? '启用' : '禁用'}</span></td>
      <td class="actions-row">
        <md-icon-button onclick="executeBackupPlan(${p.id})"><span class="material-symbols-outlined">play_arrow</span></md-icon-button>
        <md-icon-button onclick="deleteBackupPlan(${p.id},'${esc(p.name)}')"><span class="material-symbols-outlined">delete</span></md-icon-button>
      </td>
    </tr>`).join('');
  } catch (e) { console.error('Load backup plans failed:', e); }
}

async function loadBackupHistory() {
  try {
    const history = await api('/backup-history/?limit=20');
    if (!history.items || history.items.length === 0) {
      document.getElementById('backupHistoryList').innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无备份历史</p></td></tr>';
      return;
    }
    document.getElementById('backupHistoryList').innerHTML = history.items.map(h => `<tr>
      <td>${formatTime(h.created_at)}</td>
      <td>计划 #${h.backup_plan_id}</td>
      <td>${h.backup_type === 'full' ? '全量' : '增量'}</td>
      <td><span class="status-badge status-${h.status}">${statusText(h.status)}</span></td>
      <td>${formatSize(h.file_size)}</td>
      <td>${h.duration != null ? h.duration + 's' : '-'}</td>
    </tr>`).join('');
  } catch (e) { console.error('Load backup history failed:', e); }
}

function openBackupPlanDialog() {
  if (allDatabases.length === 0) { showToast('请先添加数据库', 'error'); return; }
  const dbSelect = document.getElementById('planDatabase');
  dbSelect.innerHTML = allDatabases.map(d => `<md-select-option value="${d.id}"><div slot="headline">${esc(d.name)}</div></md-select-option>`).join('');
  dbSelect.value = String(allDatabases[0].id);
  document.getElementById('planName').value = '';
  document.getElementById('planInterval').value = '60';
  document.getElementById('planCron').value = '';
  document.getElementById('planPath').value = '';
  document.getElementById('planRetention').value = '30';
  openDialog(backupPlanDialog);
}
window.openBackupPlanDialog = openBackupPlanDialog;

document.getElementById('saveBackupPlanBtn').addEventListener('click', async () => {
  const data = {
    name: document.getElementById('planName').value,
    database_id: parseInt(document.getElementById('planDatabase').value),
    backup_type: document.getElementById('planType').value,
    schedule_interval: parseInt(document.getElementById('planInterval').value) || null,
    schedule_cron: document.getElementById('planCron').value || null,
    backup_path: document.getElementById('planPath').value || null,
    retention_days: parseInt(document.getElementById('planRetention').value) || 30
  };
  if (!data.name) { showToast('请输入计划名称', 'error'); return; }
  if (!data.schedule_interval && !data.schedule_cron) { showToast('请设置备份间隔或Cron表达式', 'error'); return; }
  try {
    await api('/backup-plans/', { method: 'POST', body: JSON.stringify(data) });
    showToast('备份计划创建成功', 'success');
    closeWithAnim(backupPlanDialog);
    loadBackupPlans();
  } catch (e) {}
});

window.executeBackupPlan = async function(id) {
  try { await api(`/backup-plans/${id}/execute`, { method: 'POST' }); showToast('备份任务已提交', 'success'); } catch (e) {}
};
window.deleteBackupPlan = async function(id, name) {
  showConfirm('删除备份计划', `确定要删除计划 "${name}" 吗？`, async () => {
    try { await api(`/backup-plans/${id}`, { method: 'DELETE' }); showToast('删除成功', 'success'); loadBackupPlans(); } catch (e) {}
  });
};

// ============================================================
// Utilities
// ============================================================
function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function formatTime(iso) { if (!iso) return '-'; const d = new Date(iso); return d.toLocaleString('zh-CN'); }
function formatSize(bytes) { if (!bytes) return '-'; if (bytes < 1024) return bytes + ' B'; if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB'; if (bytes < 1024*1024*1024) return (bytes/1024/1024).toFixed(1) + ' MB'; return (bytes/1024/1024/1024).toFixed(2) + ' GB'; }
function statusText(s) { const map = { running: '运行中', stopped: '已停止', failed: '失败', pending: '等待中', completed: '已完成' }; return map[s] || s; }

// ============================================================
// Load Theme on Start
// ============================================================
async function loadTheme() {
  try {
    const theme = await api('/system/theme');
    document.getElementById('colorPicker').value = theme.primary_color || '#6750a4';
    if (theme.dark_mode) {
      isDark = true;
      document.documentElement.setAttribute('data-theme', 'dark');
      themeToggle.querySelector('span').textContent = 'light_mode';
    }
    updateTheme();
  } catch (e) { updateTheme(); }
}

// ============================================================
// Init
// ============================================================
loadTheme();
loadDashboard();