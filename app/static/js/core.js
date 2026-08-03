// ============================================================
// DBSync - Core Utils
// ============================================================
const API_BASE = '/api';

async function api(path, options = {}) {
  const url = `${API_BASE}${path}`;
  const headers = { 'Content-Type': 'application/json' };
  const token = localStorage.getItem('dbsync_token');
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const config = { headers, ...options };
  try {
    const res = await fetch(url, config);
    if (res.status === 401) {
      localStorage.removeItem('dbsync_token');
      localStorage.removeItem('dbsync_username');
      showLoginOverlay();
      throw new Error('登录已过期，请重新登录');
    }
    let data;
    const contentType = res.headers.get('content-type');
    if (contentType && contentType.includes('application/json')) {
      data = await res.json();
    } else {
      const text = await res.text();
      try { data = JSON.parse(text); } catch { data = { detail: text || `请求失败 (${res.status})` }; }
    }
    if (!res.ok) {
      const detail = data.detail;
      const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(e => e.msg || JSON.stringify(e)).join('; ') : detail ? JSON.stringify(detail) : `请求失败 (${res.status})`;
      throw new Error(msg);
    }
    return data;
  } catch (e) {
    if (e.message !== '登录已过期，请重新登录') showToast(e.message, 'error');
    throw e;
  }
}

function showToast(message, type = 'info') {
  const openDialog = document.querySelector('dialog[open]');
  const parent = openDialog || document.body;
  let container = document.getElementById('snackbar-container');
  if (!container || container.parentElement !== parent) {
    if (container) container.remove();
    container = document.createElement('div');
    container.id = 'snackbar-container';
    parent.appendChild(container);
  }
  const bar = document.createElement('div');
  bar.className = 'snackbar snackbar-' + type;
  bar.textContent = message;
  container.appendChild(bar);
  requestAnimationFrame(() => bar.classList.add('show'));
  setTimeout(() => {
    bar.classList.remove('show');
    let removed = false;
    const remove = () => { if (!removed) { removed = true; bar.remove(); } };
    bar.addEventListener('transitionend', remove, { once: true });
    setTimeout(remove, 500);
  }, 4000);
}

function showLoading(message = '处理中...') {
  const dialog = document.getElementById('loadingDialog');
  const msgEl = document.getElementById('loadingMessage');
  if (msgEl) msgEl.textContent = message;
  openDialog(dialog);
}

function hideLoading() {
  const dialog = document.getElementById('loadingDialog');
  if (dialog.open) { dialog.close(); document.body.classList.remove('dialog-open'); }
}

function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function formatTime(iso) { if (!iso) return '-'; const d = new Date(iso); return d.toLocaleString('zh-CN'); }
function formatSize(bytes) { if (!bytes) return '-'; if (bytes < 1024) return bytes + ' B'; if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB'; if (bytes < 1024*1024*1024) return (bytes/1024/1024).toFixed(1) + ' MB'; return (bytes/1024/1024/1024).toFixed(2) + ' GB'; }
function statusText(s) { const map = { running: '运行中', stopped: '已停止', failed: '失败', pending: '等待中', completed: '已完成' }; return map[s] || s; }
