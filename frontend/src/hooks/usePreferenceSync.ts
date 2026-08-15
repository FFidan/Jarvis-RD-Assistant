import { useEffect } from 'react';
import { fetchConfig, setConfig } from '@/lib/api';
import {
  ACCENT_PRESETS,
  APPEARANCE_CHANGED_EVENT,
  DENSITY_PRESETS,
  TYPE_PRESETS,
  loadAppearance,
  saveAppearance,
  type AppearancePrefs,
} from '@/lib/theme';
import { useNavPrefsStore, type NavMode } from '@/stores/nav-prefs-store';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { registerSessionReset } from '@/stores/session-reset';
import { useThemeStore, type Theme } from '@/stores/theme-store';

export const APPEARANCE_CONFIG_KEY = 'ui.appearance';
export const TIMER_CONFIG_KEY = 'ui.timer';
export const NAV_MODE_CONFIG_KEY = 'ui.nav_mode';
export const PREFERENCE_WRITE_DELAY_MS = 300;

interface ServerAppearance extends AppearancePrefs {
  theme: Theme;
}

interface TimerPreferences {
  workMinutes: number;
  shortBreakMinutes: number;
  longBreakMinutes: number;
  targetCycles: number;
}

let configRequest: ReturnType<typeof fetchConfig> | null = null;

registerSessionReset(() => {
  configRequest = null;
});

function fetchPreferencesOnce() {
  if (configRequest === null) {
    configRequest = fetchConfig().catch((error: unknown) => {
      configRequest = null;
      throw error;
    });
  }
  return configRequest;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isOneOf<T extends string>(value: unknown, options: readonly T[]): value is T {
  return typeof value === 'string' && options.includes(value as T);
}

function isAppearance(value: unknown): value is ServerAppearance {
  return (
    isRecord(value)
    && isOneOf(value.theme, ['light', 'dark', 'system'])
    && isOneOf(value.accent, ACCENT_PRESETS.map(({ id }) => id))
    && isOneOf(value.type, TYPE_PRESETS.map(({ id }) => id))
    && isOneOf(value.density, DENSITY_PRESETS.map(({ id }) => id))
  );
}

function isIntegerBetween(value: unknown, minimum: number, maximum: number): value is number {
  return Number.isInteger(value) && Number(value) >= minimum && Number(value) <= maximum;
}

function isTimerPreferences(value: unknown): value is TimerPreferences {
  return (
    isRecord(value)
    && isIntegerBetween(value.workMinutes, 15, 60)
    && isIntegerBetween(value.shortBreakMinutes, 3, 15)
    && isIntegerBetween(value.longBreakMinutes, 10, 30)
    && isIntegerBetween(value.targetCycles, 2, 8)
  );
}

function timerPreferences(): TimerPreferences {
  const state = usePomodoroStore.getState();
  return {
    workMinutes: state.workMinutes,
    shortBreakMinutes: state.shortBreakMinutes,
    longBreakMinutes: state.longBreakMinutes,
    targetCycles: state.targetCycles,
  };
}

function appearancePreferences(): ServerAppearance {
  return { theme: useThemeStore.getState().theme, ...loadAppearance() };
}

function timerPreferencesChanged(
  current: ReturnType<typeof usePomodoroStore.getState>,
  previous: ReturnType<typeof usePomodoroStore.getState>,
): boolean {
  return (
    current.workMinutes !== previous.workMinutes
    || current.shortBreakMinutes !== previous.shortBreakMinutes
    || current.longBreakMinutes !== previous.longBreakMinutes
    || current.targetCycles !== previous.targetCycles
  );
}

/** Reconcile cached preferences with the account and debounce later writes. */
export function usePreferenceSync(): void {
  useEffect(() => {
    let disposed = false;
    const timers = new Map<string, ReturnType<typeof setTimeout>>();
    const unsubscribers: Array<() => void> = [];

    const queueWrite = (key: string, value: unknown) => {
      const pending = timers.get(key);
      if (pending !== undefined) clearTimeout(pending);
      timers.set(key, setTimeout(() => {
        timers.delete(key);
        void setConfig(key, value).catch((error: unknown) => {
          console.warn('Could not sync account preference', error);
        });
      }, PREFERENCE_WRITE_DELAY_MS));
    };

    const subscribe = () => {
      if (disposed) return;
      unsubscribers.push(
        useThemeStore.subscribe((state, previous) => {
          if (state.theme !== previous.theme) queueWrite(APPEARANCE_CONFIG_KEY, appearancePreferences());
        }),
        usePomodoroStore.subscribe((state, previous) => {
          if (timerPreferencesChanged(state, previous)) queueWrite(TIMER_CONFIG_KEY, timerPreferences());
        }),
        useNavPrefsStore.subscribe((state, previous) => {
          if (state.navMode !== previous.navMode) queueWrite(NAV_MODE_CONFIG_KEY, state.navMode);
        }),
      );
      const appearanceChanged = () => queueWrite(APPEARANCE_CONFIG_KEY, appearancePreferences());
      window.addEventListener(APPEARANCE_CHANGED_EVENT, appearanceChanged);
      unsubscribers.push(() => window.removeEventListener(APPEARANCE_CHANGED_EVENT, appearanceChanged));
    };

    void fetchPreferencesOnce()
      .then((entries) => {
        if (disposed) return;
        const preferences = new Map(entries.map((entry) => [entry.key, entry.value]));
        const appearance = preferences.get(APPEARANCE_CONFIG_KEY);
        if (isAppearance(appearance)) {
          useThemeStore.getState().setTheme(appearance.theme);
          saveAppearance({
            accent: appearance.accent,
            type: appearance.type,
            density: appearance.density,
          });
        } else if (!preferences.has(APPEARANCE_CONFIG_KEY)) {
          queueWrite(APPEARANCE_CONFIG_KEY, appearancePreferences());
        }
        const timer = preferences.get(TIMER_CONFIG_KEY);
        if (isTimerPreferences(timer)) {
          usePomodoroStore.setState(timer);
        } else if (!preferences.has(TIMER_CONFIG_KEY)) {
          queueWrite(TIMER_CONFIG_KEY, timerPreferences());
        }
        const navMode = preferences.get(NAV_MODE_CONFIG_KEY);
        if (isOneOf<NavMode>(navMode, ['simple', 'full'])) {
          useNavPrefsStore.getState().setNavMode(navMode);
        } else if (!preferences.has(NAV_MODE_CONFIG_KEY)) {
          queueWrite(NAV_MODE_CONFIG_KEY, useNavPrefsStore.getState().navMode);
        }
      })
      .catch((error: unknown) => {
        console.warn('Could not load account preferences', error);
      })
      .finally(subscribe);

    return () => {
      disposed = true;
      for (const unsubscribe of unsubscribers) unsubscribe();
      for (const timer of timers.values()) clearTimeout(timer);
    };
  }, []);
}

/**
 * Mount point for {@link usePreferenceSync}.
 *
 * The sync reads and writes `/api/config`, which requires a session, so it is a
 * component rather than a call in `App` — that lets it live inside the
 * authenticated branch of the router alongside the other headless listeners.
 */
export function PreferenceSync(): null {
  usePreferenceSync();
  return null;
}
