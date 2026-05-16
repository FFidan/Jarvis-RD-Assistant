import { create } from 'zustand';
import type { SearchPreviewResult } from '@/types';

/**
 * Global store for the ⌘K command-palette overlay.
 *
 * Wired in Wave 2 (F1) so the palette is reachable from any authed page via
 * the persistent TopBar search trigger and the ⌘K / Ctrl+K shortcut. The
 * palette UI is mounted once inside CommandPaletteSearch (always present in
 * the TopBar); this store is the single source of open/query/results state.
 *
 * Mirrors keyboard-shortcuts-store.ts: a tiny create() store with an
 * initial-state constant and a _reset() called on logout to prevent
 * cross-user leakage of in-flight queries.
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
  /** Reset to initial state (called on logout and on close to drop stale results). */
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
