// ============================================================
// DBSync - Theme
// ============================================================
let isDark = false;

function generateScheme(hex, contrastLevel) {
  const { Hct, argbFromHex, hexFromArgb, SchemeTonalSpot } = window.__md3 || {};
  if (!Hct) return null;
  const argb = argbFromHex(hex);
  const hct = Hct.fromInt(argb);
  const light = new SchemeTonalSpot(hct, false, parseFloat(contrastLevel));
  const dark = new SchemeTonalSpot(hct, true, parseFloat(contrastLevel));
  return { light, dark };
}

const TOKEN_NAMES = [
  'primary','on-primary','primary-container','on-primary-container',
  'secondary','on-secondary','secondary-container','on-secondary-container',
  'tertiary','on-tertiary','tertiary-container','on-tertiary-container',
  'error','on-error','error-container','on-error-container',
  'surface','on-surface','surface-variant','on-surface-variant',
  'outline','outline-variant',
  'inverse-surface','inverse-on-surface','inverse-primary',
  'surface-dim','surface-bright',
  'surface-container-lowest','surface-container-low',
  'surface-container','surface-container-high','surface-container-highest',
];

function schemeToMap(scheme) {
  const { hexFromArgb } = window.__md3 || {};
  if (!hexFromArgb) return {};
  const map = {};
  for (const name of TOKEN_NAMES) {
    const camel = name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    let val = scheme[camel];
    if (val === undefined) {
      const alt = name.split('-').map((s, i) => i === 0 ? s : s[0].toUpperCase() + s.slice(1)).join('');
      val = scheme[alt];
    }
    map[name] = val !== undefined ? hexFromArgb(val) : '--';
  }
  return map;
}

function applyTheme(lightMap, darkMap) {
  const root = document.documentElement;
  for (const [name, hex] of Object.entries(lightMap)) {
    root.style.setProperty(`--md-sys-color-${name}`, hex);
  }
  root.dataset.darkColors = JSON.stringify(darkMap);
  if (isDark) {
    for (const [name, hex] of Object.entries(darkMap)) {
      root.style.setProperty(`--md-sys-color-${name}`, hex);
    }
  }
}

function updateTheme(save = false) {
  const hex = document.getElementById('colorPicker').value;
  const contrast = '0';
  document.getElementById('sourceHex').textContent = hex.toUpperCase();
  const scheme = generateScheme(hex, contrast);
  if (scheme) {
    applyTheme(schemeToMap(scheme.light), schemeToMap(scheme.dark));
  } else if (!window.__md3) {
    setTimeout(() => updateTheme(save), 100);
    return;
  }
  if (save) {
    api('/system/theme', { method: 'PUT', body: JSON.stringify({ primary_color: hex, dark_mode: isDark }) }).catch(() => {});
  }
}

const themeToggle = document.getElementById('themeToggle');
function toggleTheme() {
  isDark = !isDark;
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  themeToggle.querySelector('span').textContent = isDark ? 'light_mode' : 'dark_mode';
  updateTheme(true);
}
themeToggle.addEventListener('click', toggleTheme);
document.getElementById('colorPicker').addEventListener('input', () => updateTheme(true));

async function loadTheme() {
  updateTheme(false);
  try {
    const theme = await api('/system/theme');
    document.getElementById('colorPicker').value = theme.primary_color || '#6750a4';
    if (theme.dark_mode) {
      isDark = true;
      document.documentElement.setAttribute('data-theme', 'dark');
      themeToggle.querySelector('span').textContent = 'light_mode';
    }
    updateTheme(false);
  } catch (e) { /* already applied defaults */ }
}
