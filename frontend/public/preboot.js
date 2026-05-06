(function () {
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
    if (prefs.accent  && prefs.accent  !== 'ink-blue')   html.classList.add('accent-'  + prefs.accent);
    if (prefs.type    && prefs.type    !== 'serif-calm') html.classList.add('type-'    + prefs.type);
    if (prefs.density && prefs.density !== 'default')    html.classList.add('density-' + prefs.density);
  } catch (e) {}
})();
