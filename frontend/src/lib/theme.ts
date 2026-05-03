export const ACCENT_PRESETS = [
  { id: 'ink-blue',  label: 'Ink Blue',  light: '#0b3a8a', dark: '#7ba2f0' },
  { id: 'forest',    label: 'Forest',    light: '#0f5132', dark: '#6ee7b7' },
  { id: 'burgundy',  label: 'Burgundy',  light: '#7a1f2b', dark: '#fca5a5' },
  { id: 'slate',     label: 'Slate',     light: '#374151', dark: '#d1d5db' },
  { id: 'plum',      label: 'Plum',      light: '#5b1864', dark: '#d8b4fe' },
] as const;

export type AccentId = typeof ACCENT_PRESETS[number]['id'];

export const TYPE_PRESETS = [
  { id: 'serif-calm',   label: 'Serif Calm',   description: 'Source Serif 4 · JetBrains Mono' },
  { id: 'sans-modern',  label: 'Sans Modern',   description: 'Inter · IBM Plex Mono' },
  { id: 'editorial',    label: 'Editorial',     description: 'PT Serif · IBM Plex Sans' },
] as const;

export type TypeId = typeof TYPE_PRESETS[number]['id'];

export const DENSITY_PRESETS = [
  { id: 'comfortable', label: 'Comfortable' },
  { id: 'default',     label: 'Default' },
  { id: 'compact',     label: 'Compact' },
] as const;

export type DensityId = typeof DENSITY_PRESETS[number]['id'];

export interface AppearancePrefs {
  accent:  AccentId;
  type:    TypeId;
  density: DensityId;
}

const STORAGE_KEY = 'jarvis.appearance';

export function loadAppearance(): AppearancePrefs {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { ...defaultAppearance(), ...JSON.parse(raw) };
  } catch { /* ignore */ }
  return defaultAppearance();
}

export function saveAppearance(prefs: Partial<AppearancePrefs>) {
  const current = loadAppearance();
  const next = { ...current, ...prefs };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  applyAppearance(next);
}

export function applyAppearance(prefs: AppearancePrefs) {
  const html = document.documentElement;
  ACCENT_PRESETS.forEach((p) => html.classList.remove(`accent-${p.id}`));
  TYPE_PRESETS.forEach((p) => html.classList.remove(`type-${p.id}`));
  DENSITY_PRESETS.forEach((p) => html.classList.remove(`density-${p.id}`));
  html.classList.add(`accent-${prefs.accent}`);
  html.classList.add(`type-${prefs.type}`);
  html.classList.add(`density-${prefs.density}`);
}

function defaultAppearance(): AppearancePrefs {
  return { accent: 'ink-blue', type: 'serif-calm', density: 'default' };
}
