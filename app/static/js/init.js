// ============================================================
// DBSync - Init
// ============================================================
async function init() {
  await loadTheme();
  const authenticated = await checkAuth();
  if (!authenticated) return;
  loadDashboard();
}

// Backup history event delegation (Shadow DOM workaround)
setupBackupHistoryEventDelegation();

// Nav item click handlers
document.querySelectorAll('.nav-item[data-page]').forEach(item => {
  item.addEventListener('click', () => showPage(item.dataset.page));
});

// Search/filter bindings
document.getElementById('dbSearchInput')?.addEventListener('input', filterDatabases);
document.getElementById('syncSearchInput')?.addEventListener('input', filterSyncTasks);
document.getElementById('planSearchInput')?.addEventListener('input', filterBackupPlans);

// Logout
document.getElementById('logoutBtn')?.addEventListener('click', logout);

if (window.__md3) {
  init();
} else {
  document.addEventListener('md3-ready', init, { once: true });
  setTimeout(() => { if (!window.__md3) { console.warn('MD3 failed, init anyway'); init(); } }, 3000);
}
