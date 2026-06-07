import { create } from 'zustand';
import { registerSessionReset } from '@/stores/session-reset';

/**
 * Tiny global store for the KeyboardCheatSheet dialog visibility.
 *
 * Wired to make the cheat sheet reachable from any page
 * — both via the persistent TopBar icon button and via the `?` keypress
 * on the Research Feed (the only surface where shortcuts are currently
 * functional). The dialog itself is mounted once at the AppShell level.
 */
const KEYBOARD_INITIAL_STATE = { isOpen: false };

interface KeyboardShortcutsState {
  isOpen: boolean;
  open: () => void;
  close: () => void;
  toggle: () => void;
  /** Reset to initial state (called on logout to prevent cross-user leakage). */
  _reset: () => void;
}

export const useKeyboardShortcuts = create<KeyboardShortcutsState>((set) => ({
  ...KEYBOARD_INITIAL_STATE,
  open: () => set({ isOpen: true }),
  close: () => set({ isOpen: false }),
  toggle: () => set((state) => ({ isOpen: !state.isOpen })),
  _reset: () => set(KEYBOARD_INITIAL_STATE),
}));

// Reset cheat-sheet visibility on logout (see stores/session-reset).
registerSessionReset(() => useKeyboardShortcuts.getState()._reset());
