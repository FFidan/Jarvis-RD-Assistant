import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const UI_STORE_KEY = 'jarvis-ui';

interface UIState {
  sidebarCollapsed: boolean;
  selectedPaperId: number | null;
  checklistDismissed: boolean;
  /** True once the one-time onboarding-complete celebration has been shown. */
  onboardingCelebrated: boolean;
  paperDetailNoteDismissed: boolean;
  setupBannerDismissed: boolean;
  /** Last-used preset id on the Logs Events tab. Empty string = no preset. */
  logsPreset: string;
  toggleSidebar: () => void;
  setSelectedPaperId: (id: number | null) => void;
  dismissChecklist: () => void;
  markOnboardingCelebrated: () => void;
  setPaperDetailNoteDismissed: (value: boolean) => void;
  dismissSetupBanner: () => void;
  setLogsPreset: (id: string) => void;
  /** Called on auth transitions to prevent cross-user UI state leakage. */
  _reset: () => void;
}

/** Non-action defaults — used for initial state and _reset (DRY). */
const UI_INITIAL_STATE = {
  sidebarCollapsed: false,
  selectedPaperId: null as number | null,
  checklistDismissed: false,
  onboardingCelebrated: false,
  paperDetailNoteDismissed: false,
  setupBannerDismissed: false,
  logsPreset: '',
} as const;

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      ...UI_INITIAL_STATE,

      toggleSidebar() {
        set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed }));
      },
      setSelectedPaperId(id: number | null) {
        set({ selectedPaperId: id });
      },
      dismissChecklist() {
        set({ checklistDismissed: true });
      },
      markOnboardingCelebrated() {
        set({ onboardingCelebrated: true });
      },
      setPaperDetailNoteDismissed(value: boolean) {
        set({ paperDetailNoteDismissed: value });
      },
      dismissSetupBanner() {
        set({ setupBannerDismissed: true });
      },
      setLogsPreset(id: string) {
        set({ logsPreset: id });
      },
      _reset() {
        set(UI_INITIAL_STATE);
      },
    }),
    {
      name: UI_STORE_KEY,
      partialize: (state) => ({
        checklistDismissed: state.checklistDismissed,
        onboardingCelebrated: state.onboardingCelebrated,
        paperDetailNoteDismissed: state.paperDetailNoteDismissed,
        setupBannerDismissed: state.setupBannerDismissed,
        logsPreset: state.logsPreset,
      }),
    },
  ),
);
