import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const UI_STORE_KEY = 'jarvis-ui';

interface UIState {
  sidebarCollapsed: boolean;
  selectedPaperId: number | null;
  checklistDismissed: boolean;
  paperDetailNoteDismissed: boolean;
  setupBannerDismissed: boolean;
  toggleSidebar: () => void;
  setSelectedPaperId: (id: number | null) => void;
  dismissChecklist: () => void;
  setPaperDetailNoteDismissed: (value: boolean) => void;
  dismissSetupBanner: () => void;
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      selectedPaperId: null,
      checklistDismissed: false,
      paperDetailNoteDismissed: false,
      setupBannerDismissed: false,

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
    }),
    {
      name: UI_STORE_KEY,
      partialize: (state) => ({
        checklistDismissed: state.checklistDismissed,
        paperDetailNoteDismissed: state.paperDetailNoteDismissed,
        setupBannerDismissed: state.setupBannerDismissed,
      }),
    },
  ),
);
