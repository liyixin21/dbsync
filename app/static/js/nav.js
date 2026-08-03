// ============================================================
// DBSync - Navigation & Dialogs
// ============================================================
const pageTitles = { dashboard: '仪表板', databases: '数据库管理', sync: '同步管理', backup: '备份管理', logs: '日志管理', settings: '设置' };

function showPage(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item[data-page]').forEach(n => n.classList.remove('active'));
  document.getElementById(`page-${page}`)?.classList.add('active');
  document.querySelector(`.nav-item[data-page="${page}"]`)?.classList.add('active');
  document.getElementById('pageTitle').textContent = pageTitles[page] || page;
  if (page === 'dashboard') loadDashboard();
  else if (page === 'databases') loadDatabases();
  else if (page === 'sync') loadSyncTasks();
  else if (page === 'backup') { loadBackupPlans(); loadBackupHistory(); }
  else if (page === 'logs') { loadLogs(); }
  else if (page === 'settings') { loadSettings(); }
}
window.showPage = showPage;

// Drawer
const drawer = document.getElementById('drawer');
const drawerOverlay = document.getElementById('drawerOverlay');
const menubtn = document.getElementById('menubtn');
function isMobile() { return window.innerWidth <= 720; }
function toggleDrawer() {
  if (isMobile()) { drawer.classList.toggle('open'); drawerOverlay.classList.toggle('show'); }
  else { drawer.classList.toggle('collapsed'); }
}
function closeDrawer() {
  if (isMobile()) { drawer.classList.remove('open'); drawerOverlay.classList.remove('show'); }
}
menubtn.addEventListener('click', toggleDrawer);
drawerOverlay.addEventListener('click', closeDrawer);
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => { if (isMobile()) closeDrawer(); });
});

// Dialogs
function openDialog(dialog) { dialog.showModal(); document.body.classList.add('dialog-open'); }
function closeWithAnim(dialog, callback) {
  const snackbarContainer = dialog.querySelector('#snackbar-container');
  if (snackbarContainer) document.body.appendChild(snackbarContainer);
  dialog.classList.add('closing');
  setTimeout(() => {
    dialog.close(); dialog.classList.remove('closing');
    document.body.classList.remove('dialog-open');
    if (callback) callback();
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
document.getElementById('confirmDialogCancelBtn').addEventListener('click', () => { closeWithAnim(confirmDialog); confirmCallback = null; });
document.getElementById('confirmDialogOkBtn').addEventListener('click', () => {
  const cb = confirmCallback; confirmCallback = null;
  closeWithAnim(confirmDialog, cb);
});
confirmDialog.addEventListener('click', (e) => { if (e.target === confirmDialog) closeWithAnim(confirmDialog); });

function showConfirm(title, message, onConfirm) {
  document.getElementById('confirmDialogTitle').textContent = title;
  document.getElementById('confirmDialogContent').textContent = message;
  confirmCallback = onConfirm;
  openDialog(confirmDialog);
}
window.showConfirm = showConfirm;

// Loading Dialog
const loadingDialog = document.getElementById('loadingDialog');
loadingDialog.addEventListener('cancel', (e) => { e.preventDefault(); });
loadingDialog.addEventListener('click', (e) => { e.stopPropagation(); });
