import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const NAV_PREFS_STORE_KEY = 'jarvis-nav-prefs';

export const ONBOARDING_DISMISSED_KEY = 'jarvis-onboarding-dismissed';

export type NavMode = 'simple' | 'full';

interface NavPrefsState {
  navMode: NavMode;
  setNavMode: (mode: NavMode) => void;
  toggleNavMode: () => void;
}

/**
 * First-run researchers get the shorter `simple` nav so the app isn't
 * overwhelming. Existing users who already finished onboarding keep the full
 * nav — switching them to simple would be a surprise demotion. We only have to
 * decide the absent-key case: zustand/persist overwrites this with the stored
 * value whenever the key already exists.
 */
export function initialNavMode(): NavMode {
  if (typeof window === 'undefined') return 'full';
  // localStorage can throw (Safari private mode, sandboxed iframes) — degrade to
  // the non-surprising full nav rather than crashing the always-rendered sidebar.
  try {
    return localStorage.getItem(ONBOARDING_DISMISSED_KEY) === null ? 'simple' : 'full';
  } catch {
    return 'full';
  }
}

export const useNavPrefsStore = create<NavPrefsState>()(
  persist(
    (set) => ({
      navMode: initialNavMode(),
      setNavMode(mode) {
        set({ navMode: mode });
      },
      toggleNavMode() {
        set((state) => ({ navMode: state.navMode === 'simple' ? 'full' : 'simple' }));
      },
    }),
    {
      // Device-scoped: own persist key (NOT inside UI_STORE_KEY, which logout
      // removes) and deliberately NOT registered with session-reset, so the
      // chosen nav density survives logout — mirrors theme-store.
      name: NAV_PREFS_STORE_KEY,
      partialize: (state) => ({ navMode: state.navMode }),
    },
  ),
);
