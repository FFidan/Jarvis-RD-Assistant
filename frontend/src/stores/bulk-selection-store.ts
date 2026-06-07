import { create } from 'zustand';
import { registerSessionReset } from '@/stores/session-reset';

const BULK_INITIAL_STATE = { selectedIds: new Set<number>() };

interface BulkSelectionState {
  selectedIds: Set<number>;
  toggle: (id: number) => void;
  clear: () => void;
  selectMany: (ids: number[]) => void;
  /** Reset to initial state (called on logout to prevent cross-user leakage). */
  _reset: () => void;
}

export const useBulkSelection = create<BulkSelectionState>((set) => ({
  ...BULK_INITIAL_STATE,

  toggle(id: number) {
    set((state) => {
      const next = new Set(state.selectedIds);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return { selectedIds: next };
    });
  },

  clear() {
    set({ selectedIds: new Set() });
  },

  selectMany(ids: number[]) {
    set((state) => {
      const next = new Set(state.selectedIds);
      ids.forEach((id) => next.add(id));
      return { selectedIds: next };
    });
  },

  _reset() {
    set({ selectedIds: new Set<number>() });
  },
}));

// Reset bulk-selection on logout (see stores/session-reset).
registerSessionReset(() => useBulkSelection.getState()._reset());
