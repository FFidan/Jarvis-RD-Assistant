(function () {
  // Zod's object-schema fast path probes `Function()` before falling back.
  // Production's strict CSP rejects that probe, so select Zod's supported
  // interpreter path before the application bundle imports the library.
  globalThis.__zod_globalConfig = globalThis.__zod_globalConfig || {};
  globalThis.__zod_globalConfig.jitless = true;

  var html = document.documentElement;
  try {
    var raw = localStorage.getItem('jarvis-theme');
    var parsed = raw ? JSON.parse(raw) : null;
    var stored = parsed && parsed.state ? parsed.state.theme : null;
    var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var isDark = stored === 'dark' || (stored !== 'light' && prefersDark);
    if (isDark) html.classList.add('dark');
  } catch (e) {}
  try {
    var prefRaw = localStorage.getItem('jarvis.appearance');
    var prefs = prefRaw ? JSON.parse(prefRaw) : {};
    // Allowlists mirror frontend/src/lib/theme.ts ACCENT_PRESETS / TYPE_PRESETS /
    // DENSITY_PRESETS. Keep in sync with that file (preboot.js is loaded
    // pre-bundle so cannot import).
    var ACCENT_ALLOWED = ['ink-blue', 'forest', 'burgundy', 'slate', 'plum'];
    var TYPE_ALLOWED = ['serif-calm', 'sans-modern', 'editorial', 'legacy'];
    var DENSITY_ALLOWED = ['comfortable', 'default', 'compact'];
    if (prefs.accent && prefs.accent !== 'ink-blue' && ACCENT_ALLOWED.indexOf(prefs.accent) !== -1) {
      html.classList.add('accent-' + prefs.accent);
    }
    if (prefs.type && prefs.type !== 'serif-calm' && TYPE_ALLOWED.indexOf(prefs.type) !== -1) {
      html.classList.add('type-' + prefs.type);
    }
    if (prefs.density && prefs.density !== 'default' && DENSITY_ALLOWED.indexOf(prefs.density) !== -1) {
      html.classList.add('density-' + prefs.density);
    }
  } catch (e) {}
})();
