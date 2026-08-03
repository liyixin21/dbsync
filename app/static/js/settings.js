// ============================================================
// DBSync - Settings
// ============================================================
document.getElementById('changeUsernameBtn').addEventListener('click', async () => {
  const password = document.getElementById('usernamePassword').value;
  const newUsername = document.getElementById('newUsername').value;
  if (!password || !newUsername) { showToast('请填写所有字段', 'error'); return; }
  if (newUsername.length < 3) { showToast('用户名至少3个字符', 'error'); return; }
  try {
    const res = await api('/auth/change-username', { method: 'PUT', body: JSON.stringify({ password, new_username: newUsername }) });
    localStorage.setItem('dbsync_token', res.access_token);
    localStorage.setItem('dbsync_username', res.username);
    document.getElementById('currentUser').textContent = res.username;
    document.getElementById('usernamePassword').value = '';
    document.getElementById('newUsername').value = '';
    showToast('用户名修改成功', 'success');
  } catch (e) {}
});

document.getElementById('changePasswordBtn').addEventListener('click', async () => {
  const oldPassword = document.getElementById('oldPassword').value;
  const newPassword = document.getElementById('newPassword').value;
  const confirmPassword = document.getElementById('confirmPassword').value;
  if (!oldPassword || !newPassword || !confirmPassword) { showToast('请填写所有字段', 'error'); return; }
  if (newPassword.length < 6) { showToast('新密码至少6个字符', 'error'); return; }
  if (newPassword !== confirmPassword) { showToast('两次输入的新密码不一致', 'error'); return; }
  try {
    await api('/auth/change-password', { method: 'PUT', body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }) });
    document.getElementById('oldPassword').value = '';
    document.getElementById('newPassword').value = '';
    document.getElementById('confirmPassword').value = '';
    showToast('密码修改成功', 'success');
  } catch (e) {}
});

document.getElementById('saveBackupDirBtn').addEventListener('click', async () => {
  const dir = document.getElementById('backupDir').value;
  if (!dir) { showToast('请输入备份目录', 'error'); return; }
  try {
    await api('/system/configs/backup_dir', { method: 'PUT', body: JSON.stringify({ value: dir }) });
    showToast('备份目录设置成功', 'success');
  } catch (e) {}
});

async function loadSettings() {
  document.getElementById('backupDir').value = './backups';
  try {
    const token = localStorage.getItem('dbsync_token');
    const res = await fetch('/api/system/configs/backup_dir', { headers: token ? { 'Authorization': `Bearer ${token}` } : {} });
    if (res.ok) { const config = await res.json(); if (config && config.value) document.getElementById('backupDir').value = config.value; }
  } catch { /* keep default */ }
}
