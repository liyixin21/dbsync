// ============================================================
// DBSync - Backup Plans
// ============================================================
let allBackupPlans = [];

async function loadBackupPlans() {
  try {
    allBackupPlans = await api('/backup-plans/');
    if (allDatabases.length === 0) await loadDatabases();
    const dbMap = Object.fromEntries(allDatabases.map(d => [d.id, d.name]));
    if (allBackupPlans.length === 0) {
      document.getElementById('backupPlansList').innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无备份计划</p></td></tr>';
      return;
    }
    document.getElementById('backupPlansList').innerHTML = allBackupPlans.map(p => `<tr>
      <td><strong>${esc(p.name)}</strong></td><td>${esc(dbMap[p.database_id] || '?')}</td>
      <td>${p.backup_type === 'full' ? '全量' : '增量'}</td>
      <td>${p.schedule_interval ? `每 ${p.schedule_interval} 分钟` : '-'}</td>
      <td><md-switch ${p.is_active ? 'selected' : ''} onchange="toggleBackupPlanActive(${p.id}, this.selected)"></md-switch></td>
      <td class="actions-row">
        <md-fab size="small" variant="secondary" onclick="executeBackupPlan(${p.id})"><span class="material-symbols-outlined" slot="icon">play_arrow</span></md-fab>
        <md-fab size="small" onclick="editBackupPlan(${p.id})"><span class="material-symbols-outlined" slot="icon">edit</span></md-fab>
        <md-fab size="small" variant="primary" onclick="deleteBackupPlan(${p.id},'${esc(p.name)}')"><span class="material-symbols-outlined" slot="icon">delete</span></md-fab>
      </td></tr>`).join('');
  } catch (e) { console.error('Load backup plans failed:', e); }
}

function openBackupPlanDialog() {
  if (allDatabases.length === 0) { showToast('请先添加数据库', 'error'); return; }
  const dbSelect = document.getElementById('planDatabase');
  dbSelect.innerHTML = allDatabases.map(d => `<md-select-option value="${d.id}"><div slot="headline">${esc(d.name)}</div></md-select-option>`).join('');
  dbSelect.value = String(allDatabases[0].id);
  document.getElementById('planId').value = '';
  document.getElementById('planName').value = '';
  document.getElementById('planInterval').value = '60';
  document.getElementById('planRetention').value = '50';
  document.getElementById('backupPlanDialogTitle').textContent = '创建备份计划';
  openDialog(backupPlanDialog);
}
window.openBackupPlanDialog = openBackupPlanDialog;

document.getElementById('saveBackupPlanBtn').addEventListener('click', async () => {
  const id = document.getElementById('planId').value;
  const data = {
    name: document.getElementById('planName').value,
    database_id: parseInt(document.getElementById('planDatabase').value),
    backup_type: document.getElementById('planType').value,
    schedule_interval: parseInt(document.getElementById('planInterval').value) || null,
    retention_count: parseInt(document.getElementById('planRetention').value) || 50
  };
  if (!data.name) { showToast('请输入计划名称', 'error'); return; }
  if (!data.schedule_interval) { showToast('请设置备份间隔（分钟）', 'error'); return; }
  try {
    if (id) {
      await api(`/backup-plans/${id}`, { method: 'PUT', body: JSON.stringify(data) });
      showToast('备份计划更新成功', 'success');
    } else {
      await api('/backup-plans/', { method: 'POST', body: JSON.stringify(data) });
      showToast('备份计划创建成功', 'success');
    }
    closeWithAnim(backupPlanDialog); loadBackupPlans();
  } catch (e) {}
});

window.executeBackupPlan = async function(id) {
  showLoading('正在备份中...');
  try { const res = await api(`/backup-plans/${id}/execute`, { method: 'POST' }); hideLoading(); showToast(res.message || '备份已完成', 'success'); loadBackupHistory(); }
  catch (e) { hideLoading(); }
};
window.deleteBackupPlan = async function(id, name) {
  showConfirm('删除备份计划', `确定要删除计划 "${name}" 吗？`, async () => {
    try { await api(`/backup-plans/${id}`, { method: 'DELETE' }); showToast('删除成功', 'success'); loadBackupPlans(); } catch (e) {}
  });
};
window.toggleBackupPlanActive = async function(id, isActive) {
  try {
    await api(`/backup-plans/${id}`, { method: 'PUT', body: JSON.stringify({ is_active: isActive }) });
    showToast(isActive ? '备份计划已启用' : '备份计划已禁用', 'success');
    if (document.getElementById('page-dashboard').classList.contains('active')) loadDashboard();
  } catch (e) { loadBackupPlans(); }
};
window.editBackupPlan = async function(id) {
  if (allBackupPlans.length === 0) allBackupPlans = await api('/backup-plans/');
  const plan = allBackupPlans.find(p => p.id === id);
  if (!plan) return;
  if (allDatabases.length === 0) await loadDatabases();
  const dbSelect = document.getElementById('planDatabase');
  dbSelect.innerHTML = allDatabases.map(d => `<md-select-option value="${d.id}"><div slot="headline">${esc(d.name)}</div></md-select-option>`).join('');
  document.getElementById('planId').value = plan.id;
  document.getElementById('planName').value = plan.name;
  dbSelect.value = String(plan.database_id);
  document.getElementById('planType').value = plan.backup_type;
  document.getElementById('planInterval').value = plan.schedule_interval || '';
  document.getElementById('planRetention').value = plan.retention_count || 50;
  document.getElementById('backupPlanDialogTitle').textContent = '编辑备份计划';
  openDialog(backupPlanDialog);
};

function filterBackupPlans() {
  const q = document.getElementById('planSearchInput').value.toLowerCase();
  document.querySelectorAll('#backupPlansList tr').forEach(row => { row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none'; });
}
