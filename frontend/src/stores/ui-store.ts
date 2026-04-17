import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface UIState {
  sidebarCollapsed: boolean;
  selectedPaperId: number | null;
  checklistDismissed: boolean;
  paperDetailNoteDismissed: boolean;
  toggleSidebar: () => void;
  setSelectedPaperId: (id: number | null) => void;
  dismissChecklist: () => void;
  setPaperDetailNoteDismissed: (value: boolean) => void;
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      selectedPaperId: null,
      checklistDismissed: false,
      paperDetailNoteDismissed: false,

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
    }),
    {
      name: 'jarvis-ui',
      partialize: (state) => ({
        checklistDismissed: state.checklistDismissed,
        paperDetailNoteDismissed: state.paperDetailNoteDismissed,
      }),
    },
  ),
);
