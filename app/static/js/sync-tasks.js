// ============================================================
// DBSync - Sync Tasks
// ============================================================
async function loadSyncTasks() {
  try {
    const tasks = await api('/sync-tasks/');
    if (tasks.length === 0) {
      document.getElementById('syncTasksList').innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无同步任务</p></td></tr>';
      return;
    }
    if (allDatabases.length === 0) await loadDatabases();
    const dbMap = Object.fromEntries(allDatabases.map(d => [d.id, d.name]));
    document.getElementById('syncTasksList').innerHTML = tasks.map(t => `<tr>
      <td><strong>${esc(t.name)}</strong></td><td>${esc(dbMap[t.source_db_id] || '?')}</td>
      <td>${esc(dbMap[t.target_db_id] || '?')}</td>
      <td><span class="status-badge status-${t.status}">${statusText(t.status)}</span></td>
      <td>${t.last_sync_time ? formatTime(t.last_sync_time) : '-'}</td>
      <td class="actions-row">
        ${t.status === 'running'
          ? `<md-fab size="small" variant="tertiary" onclick="stopSyncTask(${t.id})"><span class="material-symbols-outlined" slot="icon">stop</span></md-fab>`
          : `<md-fab size="small" variant="secondary" onclick="startSyncTask(${t.id})"><span class="material-symbols-outlined" slot="icon">play_arrow</span></md-fab>`}
        <md-fab size="small" variant="surface" onclick="fullCopyDatabase(${t.id},'${esc(t.name)}')" title="一键复制"><span class="material-symbols-outlined" slot="icon">content_copy</span></md-fab>
        <md-fab size="small" variant="primary" onclick="deleteSyncTask(${t.id},'${esc(t.name)}')"><span class="material-symbols-outlined" slot="icon">delete</span></md-fab>
      </td></tr>`).join('');
  } catch (e) { console.error('Load sync tasks failed:', e); }
}

function openSyncTaskDialog() {
  if (allDatabases.length === 0) loadDatabases();
  if (allDatabases.length < 2) { showToast('请先添加至少两个数据库', 'error'); return; }
  const src = document.getElementById('syncSource'), tgt = document.getElementById('syncTarget');
  src.innerHTML = allDatabases.map(d => `<md-select-option value="${d.id}"><div slot="headline">${esc(d.name)}</div></md-select-option>`).join('');
  tgt.innerHTML = allDatabases.map(d => `<md-select-option value="${d.id}"><div slot="headline">${esc(d.name)}</div></md-select-option>`).join('');
  src.value = String(allDatabases[0].id); tgt.value = String(allDatabases[1].id);
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
    showToast('同步任务创建成功', 'success'); closeWithAnim(syncTaskDialog); loadSyncTasks();
  } catch (e) {}
});

window.startSyncTask = async function(id) {
  showLoading('正在开始同步...');
  try { const res = await api(`/sync-tasks/${id}/start`, { method: 'POST' }); hideLoading(); showToast(res.message || '同步任务已启动', 'success'); loadSyncTasks(); }
  catch (e) { hideLoading(); }
};
window.stopSyncTask = async function(id) {
  showLoading('正在停止同步...');
  try { const res = await api(`/sync-tasks/${id}/stop`, { method: 'POST' }); hideLoading(); showToast(res.message || '同步任务已停止', 'success'); loadSyncTasks(); }
  catch (e) { hideLoading(); }
};
window.deleteSyncTask = async function(id, name) {
  showConfirm('删除同步任务', `确定要删除任务 "${name}" 吗？`, async () => {
    try { await api(`/sync-tasks/${id}`, { method: 'DELETE' }); showToast('删除成功', 'success'); loadSyncTasks(); } catch (e) {}
  });
};
window.fullCopyDatabase = async function(id, name) {
  showConfirm('一键复制', '确定要将源数据库的所有数据完整复制到目标数据库吗？\n\n⚠️ 此操作将覆盖目标数据库中的所有数据！', async () => {
    showLoading('正在全量复制...');
    try { const res = await api(`/sync-tasks/${id}/full-copy`, { method: 'POST' }); hideLoading(); showToast(res.message || '全量复制完成', 'success'); loadSyncTasks(); }
    catch (e) { hideLoading(); }
  });
};

function filterSyncTasks() {
  const q = document.getElementById('syncSearchInput').value.toLowerCase();
  document.querySelectorAll('#syncTasksList tr').forEach(row => { row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none'; });
}
