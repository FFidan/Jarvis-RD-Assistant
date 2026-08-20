import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchConfig, setConfig } from '@/lib/api';
import { loadAppearance, saveAppearance } from '@/lib/theme';
import {
  APPEARANCE_CONFIG_KEY,
  NAV_MODE_CONFIG_KEY,
  PREFERENCE_WRITE_DELAY_MS,
  TIMER_CONFIG_KEY,
  usePreferenceSync,
} from '@/hooks/usePreferenceSync';
import { useNavPrefsStore } from '@/stores/nav-prefs-store';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { useThemeStore, THEME_STORE_KEY } from '@/stores/theme-store';

vi.mock('@/lib/api', () => ({
  fetchConfig: vi.fn(),
  setConfig: vi.fn(),
}));

describe('theme-store', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useThemeStore.setState({ theme: 'system' });
    useNavPrefsStore.setState({ navMode: 'simple' });
    usePomodoroStore.setState({
      workMinutes: 25,
      shortBreakMinutes: 5,
      longBreakMinutes: 15,
      targetCycles: 4,
    });
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

  it('reconciles from the server and debounces one write for a later change', async () => {
    vi.useFakeTimers();
    saveAppearance({ accent: 'forest', type: 'legacy', density: 'compact' });
    useThemeStore.getState().setTheme('dark');

    let resolveConfig!: (value: Awaited<ReturnType<typeof fetchConfig>>) => void;
    vi.mocked(fetchConfig).mockImplementationOnce(
      () => new Promise((resolve) => { resolveConfig = resolve; }),
    );
    vi.mocked(setConfig).mockResolvedValue({ key: APPEARANCE_CONFIG_KEY, value: null });

    const { unmount } = renderHook(() => usePreferenceSync());
    await act(async () => {
      resolveConfig([
        {
          key: APPEARANCE_CONFIG_KEY,
          value: { theme: 'light', accent: 'plum', type: 'editorial', density: 'comfortable' },
        },
        {
          key: TIMER_CONFIG_KEY,
          value: { workMinutes: 50, shortBreakMinutes: 10, longBreakMinutes: 25, targetCycles: 6 },
        },
        { key: NAV_MODE_CONFIG_KEY, value: 'full' },
      ]);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(useThemeStore.getState().theme).toBe('light');
    expect(loadAppearance()).toEqual({
      accent: 'plum',
      type: 'editorial',
      density: 'comfortable',
    });
    expect(usePomodoroStore.getState()).toMatchObject({
      workMinutes: 50,
      shortBreakMinutes: 10,
      longBreakMinutes: 25,
      targetCycles: 6,
    });
    expect(useNavPrefsStore.getState().navMode).toBe('full');
    expect(setConfig).not.toHaveBeenCalled();

    act(() => useThemeStore.getState().setTheme('dark'));
    act(() => vi.advanceTimersByTime(PREFERENCE_WRITE_DELAY_MS - 1));
    expect(setConfig).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });

    expect(setConfig).toHaveBeenCalledOnce();
    expect(setConfig).toHaveBeenCalledWith(APPEARANCE_CONFIG_KEY, {
      theme: 'dark',
      accent: 'plum',
      type: 'editorial',
      density: 'comfortable',
    });

    unmount();
    vi.useRealTimers();
  });
});
