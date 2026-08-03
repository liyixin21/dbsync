// ============================================================
// DBSync - Databases
// ============================================================
let allDatabases = [];
let dbConnectionTested = false;

async function loadDatabases() {
  try {
    allDatabases = await api('/databases/');
    const tbody = document.getElementById('databasesList');
    if (allDatabases.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-state"><p>暂无数据库配置，点击"添加数据库"开始</p></td></tr>';
      return;
    }
    tbody.innerHTML = allDatabases.map(db => `<tr>
      <td><strong>${esc(db.name)}</strong></td><td>${esc(db.host)}</td><td>${db.port}</td>
      <td>${esc(db.database_name)}</td>
      <td><span class="status-badge ${db.is_active ? 'status-running' : 'status-stopped'}">${db.is_active ? '启用' : '禁用'}</span></td>
      <td class="actions-row">
        <md-fab size="small" onclick="editDatabase(${db.id})"><span class="material-symbols-outlined" slot="icon">edit</span></md-fab>
        <md-fab size="small" variant="primary" onclick="deleteDatabase(${db.id},'${esc(db.name)}')"><span class="material-symbols-outlined" slot="icon">delete</span></md-fab>
      </td></tr>`).join('');
  } catch (e) { console.error('Load databases failed:', e); }
}

function openDatabaseDialog(db = null) {
  dbConnectionTested = false;
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

window.editDatabase = async function(id) { const db = allDatabases.find(d => d.id === id); if (db) openDatabaseDialog(db); };

document.getElementById('saveDatabaseBtn').addEventListener('click', async () => {
  if (!dbConnectionTested) { showToast('请先测试数据库连接', 'error'); return; }
  const id = document.getElementById('dbId').value;
  const data = {
    name: document.getElementById('dbName').value, host: document.getElementById('dbHost').value,
    port: parseInt(document.getElementById('dbPort').value), username: document.getElementById('dbUsername').value,
    password: document.getElementById('dbPassword').value, database_name: document.getElementById('dbDatabaseName').value
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
    closeWithAnim(databaseDialog); loadDatabases();
  } catch (e) {}
});

document.getElementById('testConnectionBtn').addEventListener('click', async () => {
  const data = {
    host: document.getElementById('dbHost').value, port: parseInt(document.getElementById('dbPort').value),
    username: document.getElementById('dbUsername').value, password: document.getElementById('dbPassword').value,
    database_name: document.getElementById('dbDatabaseName').value
  };
  if (!data.host || !data.username || !data.database_name) { showToast('请填写主机、用户名和数据库名', 'error'); return; }
  try {
    const res = await api('/databases/test-connection', { method: 'POST', body: JSON.stringify(data) });
    showToast(res.message, res.success ? 'success' : 'error');
    dbConnectionTested = res.success;
  } catch (e) { dbConnectionTested = false; }
});

window.deleteDatabase = async function(id, name) {
  showConfirm('删除数据库', `确定要删除数据库 "${name}" 吗？`, async () => {
    try { await api(`/databases/${id}`, { method: 'DELETE' }); showToast('删除成功', 'success'); loadDatabases(); } catch (e) {}
  });
};

function filterDatabases() {
  const q = document.getElementById('dbSearchInput').value.toLowerCase();
  document.querySelectorAll('#databasesList tr').forEach(row => { row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none'; });
}
