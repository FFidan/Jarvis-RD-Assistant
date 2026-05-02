import { describe, it, expect, beforeEach } from 'vitest';
import { useThemeStore, THEME_STORE_KEY } from '@/stores/theme-store';

describe('theme-store', () => {
  beforeEach(() => {
    localStorage.clear();
    useThemeStore.setState({ theme: 'system' });
  });

  it('THEME_STORE_KEY equals jarvis-theme', () => {
    expect(THEME_STORE_KEY).toBe('jarvis-theme');
  });

  it('initial state has theme = system', () => {
    expect(useThemeStore.getState().theme).toBe('system');
  });

  it('setTheme writes to localStorage', () => {
    useThemeStore.getState().setTheme('dark');
    const raw = localStorage.getItem('jarvis-theme');
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.state.theme).toBe('dark');
  });

  it('cycleTheme rotates light → dark → system → light', () => {
    useThemeStore.setState({ theme: 'light' });

    useThemeStore.getState().cycleTheme();
    expect(useThemeStore.getState().theme).toBe('dark');

    useThemeStore.getState().cycleTheme();
    expect(useThemeStore.getState().theme).toBe('system');

    useThemeStore.getState().cycleTheme();
    expect(useThemeStore.getState().theme).toBe('light');
  });
});
