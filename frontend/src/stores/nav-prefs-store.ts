import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const NAV_PREFS_STORE_KEY = 'jarvis-nav-prefs';

export type NavMode = 'simple' | 'full';

interface NavPrefsState {
  navMode: NavMode;
  setNavMode: (mode: NavMode) => void;
  toggleNavMode: () => void;
}

/**
 * The short `simple` rail is the nav everyone starts on: it carries the daily
 * research loop and the full grouped view is one toggle away. This value only
 * decides the case where nothing has been stored yet — zustand/persist
 * overwrites it with the saved preference whenever the key already exists.
 */
export function initialNavMode(): NavMode {
  return 'simple';
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
      // Account-backed with a dedicated local cache: the saved value provides
      // the first paint, survives logout, and is reconciled after sign-in.
      name: NAV_PREFS_STORE_KEY,
      partialize: (state) => ({ navMode: state.navMode }),
    },
  ),
);
