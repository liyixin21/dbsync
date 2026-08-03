// ============================================================
// DBSync - Dashboard
// ============================================================
async function loadDashboard() {
  try {
    const [dbList, syncList, planList] = await Promise.all([
      api('/databases/'), api('/sync-tasks/'), api('/backup-plans/')
    ]);
    document.getElementById('databasesCount').textContent = dbList.length;
    document.getElementById('syncTasksCount').textContent = syncList.filter(t => t.status === 'running').length;
    document.getElementById('backupPlansCount').textContent = planList.filter(p => p.is_active).length;
    try {
      const history = await api('/backup-history/?limit=5');
      const tbody = document.getElementById('recentBackups');
      if (history.items && history.items.length > 0) {
        tbody.innerHTML = history.items.map(h => `<tr>
          <td>${formatTime(h.created_at)}</td><td>计划 #${h.backup_plan_id}</td>
          <td>${h.backup_type === 'full' ? '全量' : '增量'}</td>
          <td><span class="status-badge status-${h.status}">${statusText(h.status)}</span></td>
          <td>${formatSize(h.file_size)}</td></tr>`).join('');
      }
      const stats = await api('/backup-history/statistics');
      document.getElementById('backupSize').textContent = formatSize(stats.total_size);
    } catch (e) { /* ignore */ }
  } catch (e) { console.error('Dashboard load failed:', e); }
}
