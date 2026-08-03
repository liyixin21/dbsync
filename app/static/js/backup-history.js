// ============================================================
// DBSync - Backup History
// ============================================================
let backupHistoryPage = 0;
const backupHistoryPageSize = 20;

async function loadBackupHistory(page) {
  if (page === undefined) page = backupHistoryPage;
  backupHistoryPage = page;
  const skip = page * backupHistoryPageSize;
  try {
    const history = await api(`/backup-history/?skip=${skip}&limit=${backupHistoryPageSize}`);
    const tbody = document.getElementById('backupHistoryList');
    if (!history.items || history.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-state"><p>暂无备份历史</p></td></tr>';
      renderPagination(0, page);
      return;
    }
    tbody.innerHTML = history.items.map(h => `<tr>
      <td>${formatTime(h.created_at)}</td><td>计划 #${h.backup_plan_id}</td>
      <td>${h.backup_type === 'full' ? '全量' : '增量'}</td>
      <td><span class="status-badge status-${h.status}">${statusText(h.status)}</span></td>
      <td>${formatSize(h.file_size)}</td><td>${h.duration != null ? h.duration + 's' : '-'}</td>
      <td class="actions-row">
        ${h.status === 'completed' && h.file_path ? `<md-fab size="small" variant="surface" data-action="download" data-id="${h.id}" title="下载"><span class="material-symbols-outlined" slot="icon">download</span></md-fab>
        <md-fab size="small" variant="secondary" data-action="restore" data-id="${h.id}" title="恢复"><span class="material-symbols-outlined" slot="icon">settings_backup_restore</span></md-fab>` : ''}
        <md-fab size="small" variant="primary" data-action="delete" data-id="${h.id}" title="删除"><span class="material-symbols-outlined" slot="icon">delete</span></md-fab>
      </td></tr>`).join('');
    renderPagination(history.total, page);
  } catch (e) { console.error('Load backup history failed:', e); }
}

function renderPagination(total, currentPage) {
  const container = document.getElementById('backupHistoryPagination');
  if (!container) return;
  const totalPages = Math.ceil(total / backupHistoryPageSize);
  if (totalPages <= 1) { container.innerHTML = ''; return; }
  let html = '';
  if (currentPage > 0) html += `<md-text-button onclick="loadBackupHistory(${currentPage - 1})">上一页</md-text-button>`;
  html += `<span style="padding:0 12px;align-self:center">第 ${currentPage + 1} / ${totalPages} 页</span>`;
  if (currentPage < totalPages - 1) html += `<md-text-button onclick="loadBackupHistory(${currentPage + 1})">下一页</md-text-button>`;
  container.innerHTML = html;
}

window.downloadBackup = async function(id) {
  try {
    const token = localStorage.getItem('dbsync_token');
    const res = await fetch(`/api/backup-history/${id}/download`, {
      headers: token ? { 'Authorization': `Bearer ${token}` } : {}
    });
    if (!res.ok) throw new Error('下载失败');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `backup_${id}.sql`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) { showToast('下载失败', 'error'); }
};

window.restoreBackup = async function(id) {
  showConfirm('恢复数据库', '确定要从此备份恢复数据库吗？此操作不可撤销。', async () => {
    showLoading('正在恢复备份...');
    try { const res = await api(`/backup-history/${id}/restore`, { method: 'POST', body: '{}' }); hideLoading(); showToast(res.message || '恢复成功', 'success'); loadBackupHistory(); }
    catch (e) { hideLoading(); }
  });
};

window.deleteBackupHistory = async function(id) {
  showConfirm('删除备份', '确定要删除此备份记录吗？如果存在备份文件也将被删除。', async () => {
    try { const res = await api(`/backup-history/${id}`, { method: 'DELETE' }); showToast(res.message || '删除成功', 'success'); loadBackupHistory(); } catch (e) {}
  });
};

function setupBackupHistoryEventDelegation() {
  const tbody = document.getElementById('backupHistoryList');
  if (!tbody) return;
  tbody.addEventListener('click', (e) => {
    const fab = e.target.closest('md-fab[data-action]');
    if (!fab) return;
    const action = fab.dataset.action, id = parseInt(fab.dataset.id);
    if (isNaN(id)) return;
    if (action === 'download') downloadBackup(id);
    else if (action === 'restore') restoreBackup(id);
    else if (action === 'delete') deleteBackupHistory(id);
  });
}

window.clearBackupHistory = function() {
  showConfirm('清除备份历史', '确定要清除所有备份历史记录吗？不会删除实际备份文件。', async () => {
    try { const res = await api('/backup-history/clear', { method: 'DELETE' }); showToast(res.message || '备份历史已清除', 'success'); loadDashboard(); } catch (e) {}
  });
};
