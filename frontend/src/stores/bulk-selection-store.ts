import { create } from 'zustand';

interface BulkSelectionState {
  selectedIds: Set<number>;
  toggle: (id: number) => void;
  clear: () => void;
  selectMany: (ids: number[]) => void;
}

export const useBulkSelection = create<BulkSelectionState>((set) => ({
  selectedIds: new Set(),

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
}));
