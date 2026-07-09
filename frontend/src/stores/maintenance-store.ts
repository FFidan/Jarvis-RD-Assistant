import { create } from 'zustand';

/**
 * Global maintenance-mode flag, flipped by the `_doFetch` 503 interceptor
 * (lib/api/core.ts) and cleared by MaintenanceBanner once a health poll
 * reports the restore is over. Global/ephemeral server-side state, not
 * per-user session state — deliberately NOT registered with session-reset
 * (mirrors nav-prefs-store: a mid-restore logout must not drop the banner).
 */
interface MaintenanceState {
  active: boolean;
  retryAfterS: number | null;
  since: number | null;
  setMaintenance: (active: boolean, retryAfterS: number) => void;
  clear: () => void;
}

export const useMaintenanceStore = create<MaintenanceState>((set) => ({
  active: false,
  retryAfterS: null,
  since: null,
  setMaintenance: (active, retryAfterS) =>
    set((state) => ({
      active,
      retryAfterS,
      since: active ? state.since ?? Date.now() : null,
    })),
  clear: () => set({ active: false, retryAfterS: null, since: null }),
}));
