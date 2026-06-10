import { create } from 'zustand';
import type { SearchPreviewResult } from '@/types';
import { registerSessionReset } from '@/stores/session-reset';

/**
 * Global store for the ⌘K command-palette overlay.
 *
 * Wired so the palette is reachable from any authed page via
 * the persistent TopBar search trigger and the ⌘K / Ctrl+K shortcut. The
 * palette UI is mounted once inside CommandPaletteSearch (always present in
 * the TopBar); this store is the single source of open/query/results state.
 *
 * Mirrors keyboard-shortcuts-store.ts: a tiny create() store with an
 * initial-state constant and a _reset() method that fully clears store
 * state (available for tests and future logout wiring; not currently
 * called on logout — the store has no persist middleware and close()
 * already resets visible state on each palette interaction).
 */
interface CommandPaletteSnapshot {
  isOpen: boolean;
  query: string;
  results: SearchPreviewResult[];
  loading: boolean;
  /** True when the last search failed (network/server). Drives the friendly error state. */
  errored: boolean;
}

const COMMAND_PALETTE_INITIAL_STATE: CommandPaletteSnapshot = {
  isOpen: false,
  query: '',
  results: [],
  loading: false,
  errored: false,
};

interface CommandPaletteState extends CommandPaletteSnapshot {
  open: () => void;
  close: () => void;
  toggle: () => void;
  setQuery: (query: string) => void;
  setLoading: (loading: boolean) => void;
  setResults: (results: SearchPreviewResult[]) => void;
  setErrored: (errored: boolean) => void;
  /** Fully resets store to initial state. Used in tests; available for future logout wiring. */
  _reset: () => void;
}

export const useCommandPalette = create<CommandPaletteState>((set) => ({
  ...COMMAND_PALETTE_INITIAL_STATE,
  open: () => set({ isOpen: true }),
  close: () => set(COMMAND_PALETTE_INITIAL_STATE),
  toggle: () =>
    set((state) =>
      state.isOpen ? COMMAND_PALETTE_INITIAL_STATE : { isOpen: true },
    ),
  setQuery: (query) => set({ query }),
  setLoading: (loading) => set({ loading }),
  setResults: (results) => set({ results }),
  setErrored: (errored) => set({ errored }),
  _reset: () => set(COMMAND_PALETTE_INITIAL_STATE),
}));

// Reset command-palette state on logout (see stores/session-reset).
registerSessionReset(() => useCommandPalette.getState()._reset());
