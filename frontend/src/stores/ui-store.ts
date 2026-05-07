import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const UI_STORE_KEY = 'jarvis-ui';

export type HeroMode = 'pulse' | 'task' | 'reading';

interface UIState {
  sidebarCollapsed: boolean;
  selectedPaperId: number | null;
  checklistDismissed: boolean;
  paperDetailNoteDismissed: boolean;
  setupBannerDismissed: boolean;
  heroMode: HeroMode;
  toggleSidebar: () => void;
  setSelectedPaperId: (id: number | null) => void;
  dismissChecklist: () => void;
  setPaperDetailNoteDismissed: (value: boolean) => void;
  dismissSetupBanner: () => void;
  setHeroMode: (mode: HeroMode) => void;
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      selectedPaperId: null,
      checklistDismissed: false,
      paperDetailNoteDismissed: false,
      setupBannerDismissed: false,
      heroMode: 'pulse',

      toggleSidebar() {
        set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed }));
      },
      setSelectedPaperId(id: number | null) {
        set({ selectedPaperId: id });
      },
      dismissChecklist() {
        set({ checklistDismissed: true });
      },
      setPaperDetailNoteDismissed(value: boolean) {
        set({ paperDetailNoteDismissed: value });
      },
      dismissSetupBanner() {
        set({ setupBannerDismissed: true });
      },
      setHeroMode(mode: HeroMode) {
        set({ heroMode: mode });
      },
    }),
    {
      name: UI_STORE_KEY,
      partialize: (state) => ({
        checklistDismissed: state.checklistDismissed,
        paperDetailNoteDismissed: state.paperDetailNoteDismissed,
        setupBannerDismissed: state.setupBannerDismissed,
        heroMode: state.heroMode,
      }),
    },
  ),
);
