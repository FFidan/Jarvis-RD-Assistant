import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const THEME_STORE_KEY = 'jarvis-theme';

export type Theme = 'light' | 'dark' | 'system';

interface ThemeState {
  theme: Theme;
  setTheme: (t: Theme) => void;
  cycleTheme: () => void; // light → dark → system → light
}

export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      theme: 'system',
      setTheme(t) {
        set({ theme: t });
      },
      cycleTheme() {
        set((state) => {
          const next: Theme =
            state.theme === 'light' ? 'dark' : state.theme === 'dark' ? 'system' : 'light';
          return { theme: next };
        });
      },
    }),
    {
      name: THEME_STORE_KEY,
      partialize: (state) => ({ theme: state.theme }),
    },
  ),
);
