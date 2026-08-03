// ============================================================
// DBSync - Authentication
// ============================================================
function showLoginOverlay() {
  document.getElementById('loginOverlay').classList.remove('hidden');
  document.getElementById('appShell').style.display = 'none';
}

function hideLoginOverlay() {
  document.getElementById('loginOverlay').classList.add('hidden');
  document.getElementById('appShell').style.display = 'flex';
}

async function login() {
  const username = document.getElementById('loginUsername').value.trim();
  const password = document.getElementById('loginPassword').value;
  const errorEl = document.getElementById('loginError');
  if (!username || !password) { errorEl.textContent = '请输入用户名和密码'; return; }
  errorEl.textContent = '';
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if (!res.ok) { errorEl.textContent = data.detail || '登录失败'; return; }
    localStorage.setItem('dbsync_token', data.access_token);
    localStorage.setItem('dbsync_username', data.username);
    document.getElementById('currentUser').textContent = data.username;
    hideLoginOverlay();
    showToast('登录成功', 'success');
    init();
  } catch (e) { errorEl.textContent = '网络错误，请重试'; }
}

function logout() {
  localStorage.removeItem('dbsync_token');
  localStorage.removeItem('dbsync_username');
  showLoginOverlay();
  showToast('已退出登录', 'info');
}

async function checkAuth() {
  const token = localStorage.getItem('dbsync_token');
  if (!token) { showLoginOverlay(); return false; }
  try {
    const res = await fetch('/api/auth/me', { headers: { 'Authorization': `Bearer ${token}` } });
    if (res.ok) {
      const user = await res.json();
      document.getElementById('currentUser').textContent = user.username;
      hideLoginOverlay();
      return true;
    }
    localStorage.removeItem('dbsync_token');
    localStorage.removeItem('dbsync_username');
    showLoginOverlay();
    return false;
  } catch (e) { showLoginOverlay(); return false; }
}

document.getElementById('loginBtn').addEventListener('click', login);
document.getElementById('loginForm').addEventListener('submit', (e) => { e.preventDefault(); login(); });
document.getElementById('loginPassword').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
document.getElementById('loginUsername').addEventListener('keydown', (e) => { if (e.key === 'Enter') document.getElementById('loginPassword').focus(); });
