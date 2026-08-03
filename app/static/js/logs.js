// ============================================================
// DBSync - Logs
// ============================================================
let currentLogType = 'operations', logsPage = 0;
const logsPageSize = 30;

function switchLogType() {
  currentLogType = document.getElementById('logTypeSelect').value; logsPage = 0;
  document.getElementById('operationLogsSection').style.display = currentLogType === 'operations' ? '' : 'none';
  document.getElementById('loginLogsSection').style.display = currentLogType === 'logins' ? '' : 'none';
  document.getElementById('runLogsSection').style.display = currentLogType === 'runs' ? '' : 'none';
  loadLogs();
}
window.switchLogType = switchLogType;
function filterLogs() { logsPage = 0; loadLogs(); }
window.filterLogs = filterLogs;

async function loadLogs(page) {
  if (page === undefined) page = logsPage; logsPage = page;
  const skip = page * logsPageSize;
  const search = document.getElementById('logSearchInput').value.trim();
  try {
    let url = `/logs/${currentLogType}?skip=${skip}&limit=${logsPageSize}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    const data = await api(url);
    if (currentLogType === 'operations') renderOperationLogs(data.items);
    else if (currentLogType === 'logins') renderLoginLogs(data.items);
    else if (currentLogType === 'runs') renderRunLogs(data.items);
    renderLogsPagination(data.total);
  } catch (e) { console.error('Load logs failed:', e); }
}

function renderOperationLogs(items) {
  const tbody = document.getElementById('operationLogsList');
  if (!items || items.length === 0) { tbody.innerHTML = '<tr><td colspan="7" class="empty-state"><p>暂无操作日志</p></td></tr>'; return; }
  tbody.innerHTML = items.map(log => `<tr>
    <td>${formatTime(log.created_at)}</td><td>${esc(log.username || '-')}</td><td>${esc(log.action)}</td>
    <td>${log.resource_type ? `<span class="log-type-badge">${esc(log.resource_type)}</span>` : '-'}</td>
    <td>${esc(log.detail || log.resource_name || '-')}</td><td>${esc(log.ip_address || '-')}</td>
    <td class="actions-row"><md-fab size="small" variant="primary" onclick="deleteLog('operations',${log.id})"><span class="material-symbols-outlined" slot="icon">delete</span></md-fab></td>
  </tr>`).join('');
}

function renderLoginLogs(items) {
  const tbody = document.getElementById('loginLogsList');
  if (!items || items.length === 0) { tbody.innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无登录日志</p></td></tr>'; return; }
  tbody.innerHTML = items.map(log => `<tr>
    <td>${formatTime(log.created_at)}</td><td>${esc(log.username)}</td>
    <td><span class="${log.success ? 'login-success' : 'login-fail'}">${log.success ? '成功' : '失败'}</span></td>
    <td>${esc(log.ip_address || '-')}</td><td>${esc(log.failure_reason || '-')}</td>
    <td class="actions-row"><md-fab size="small" variant="primary" onclick="deleteLog('logins',${log.id})"><span class="material-symbols-outlined" slot="icon">delete</span></md-fab></td>
  </tr>`).join('');
}

function renderRunLogs(items) {
  const tbody = document.getElementById('runLogsList');
  if (!items || items.length === 0) { tbody.innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无运行日志</p></td></tr>'; return; }
  tbody.innerHTML = items.map(log => `<tr>
    <td>${formatTime(log.created_at)}</td><td><span class="log-type-badge">${esc(log.task_type)}</span></td>
    <td>${esc(log.task_name || `#${log.task_id}`)}</td>
    <td><span class="log-level-badge log-level-${log.level.toLowerCase()}">${esc(log.level)}</span></td>
    <td>${esc(log.message)}</td>
    <td class="actions-row"><md-fab size="small" variant="primary" onclick="deleteLog('runs',${log.id})"><span class="material-symbols-outlined" slot="icon">delete</span></md-fab></td>
  </tr>`).join('');
}

function renderLogsPagination(total) {
  const container = document.getElementById('logsPagination');
  if (!container) return;
  const totalPages = Math.ceil(total / logsPageSize);
  if (totalPages <= 1) { container.innerHTML = ''; return; }
  let html = '';
  if (logsPage > 0) html += `<md-text-button onclick="loadLogsPage(${logsPage - 1})">上一页</md-text-button>`;
  html += `<span style="padding:0 12px;align-self:center">第 ${logsPage + 1} / ${totalPages} 页</span>`;
  if (logsPage < totalPages - 1) html += `<md-text-button onclick="loadLogsPage(${logsPage + 1})">下一页</md-text-button>`;
  container.innerHTML = html;
}
window.loadLogsPage = loadLogsPage;

async function deleteLog(type, id) {
  try { await api(`/logs/${type}/${id}`, { method: 'DELETE' }); showToast('删除成功', 'success'); loadLogs(); } catch (e) {}
}
window.deleteLog = deleteLog;

window.clearAllLogs = function() {
  const typeNames = { operations: '操作日志', logins: '登录日志', runs: '运行日志' };
  showConfirm('清空日志', `确定要清空所有${typeNames[currentLogType]}吗？此操作不可撤销。`, async () => {
    try { await api(`/logs/${currentLogType}-clear`, { method: 'DELETE' }); showToast('日志已清空', 'success'); loadLogs(); } catch (e) {}
  });
};
