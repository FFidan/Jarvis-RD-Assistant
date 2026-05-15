import { create } from 'zustand';

/**
 * Tiny global store for the KeyboardCheatSheet dialog visibility.
 *
 * Wired in Wave 7 (B.6) to make the cheat sheet reachable from any page
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
