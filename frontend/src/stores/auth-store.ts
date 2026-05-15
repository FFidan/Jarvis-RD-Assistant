import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';
import type { QueryClient } from '@tanstack/react-query';
import { UI_STORE_KEY } from '@/stores/ui-store';
import { abortAllStreams } from '@/stores/chat-store';

// ---------------------------------------------------------------------------
// QueryClient holder — registered once by the app's provider tree so logout()
// can clear the React-Query cache without creating a circular import.
// ---------------------------------------------------------------------------
let _queryClient: QueryClient | null = null;

export function registerQueryClient(qc: QueryClient): void {
  _queryClient = qc;
}

/**
 * Two coexisting authentication paths:
 *
 * 1. Magic-link sessions (Phase 2 WS-2A): the backend issues a session cookie
 *    after /api/auth/verify; the cookie is HttpOnly so the browser can't read
 *    it. This store carries only the user record (id/email/role) returned by
 *    /api/auth/verify so the UI can render greetings + role-gated chrome. The
 *    cookie travels on every fetch automatically (credentials:'include' on
 *    apiFetch).
 *
 * 2. API-key sessions (legacy): users sitting in front of the wizard or
 *    self-hosters who haven't set up SMTP can still paste their JARVIS_API_KEY
 *    into the login form. The key is stored in sessionStorage and threaded
 *    through every fetch as X-API-Key. WS-2A keeps this path working so
 *    Telegram bot + non-browser callers don't break and so dev-mode iteration
 *    on a fresh install still works without an SMTP relay.
 *
 * When both a session cookie AND an X-API-Key are present the backend prefers
 * the session cookie (verify_api_key skips /api/auth/* and the session
 * middleware runs unconditionally).
 */
const SESSION_DURATION_MS = 8 * 60 * 60 * 1000; // 8 hours

export interface SessionUser {
  id: number;
  email: string;
  role: 'user' | 'admin';
}

interface AuthState {
  isAuthenticated: boolean;
  authTime: number | null;
  apiKey: string | null;
  user: SessionUser | null;
  login: (apiKey: string) => Promise<boolean>;
  loginWithSession: (user: SessionUser) => void;
  logout: () => void;
  checkSession: () => boolean;
  getApiKey: () => string | null;
  getUser: () => SessionUser | null;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      isAuthenticated: false,
      authTime: null,
      apiKey: null,
      user: null,

      async login(apiKey: string): Promise<boolean> {
        try {
          // Validate the API key against the backend
          const res = await fetch('/api/topics', {
            headers: { 'X-API-Key': apiKey },
          });
          if (res.ok || res.status === 200) {
            set({ isAuthenticated: true, authTime: Date.now(), apiKey, user: null });
            return true;
          }
          return false;
        } catch {
          return false;
        }
      },

      loginWithSession(user: SessionUser): void {
        // Magic-link path: the session cookie is already set by the backend.
        // We persist nothing security-sensitive here — just the user record so
        // the UI can render. The cookie is HttpOnly so the browser/JS can't
        // read or forge it; checkSession still gates on authTime so a stale
        // tab doesn't pretend to be logged in forever after the cookie
        // expires server-side.
        set({
          isAuthenticated: true,
          authTime: Date.now(),
          apiKey: null,
          user,
        });
      },

      logout() {
        // Abort any in-flight SSE streams before clearing session state.
        abortAllStreams();

        // Clear the UI store's persisted localStorage entry so a fresh login
        // doesn't inherit stale UI state from a previous session.
        localStorage.removeItem(UI_STORE_KEY);

        // Clear auth state first.
        set({ isAuthenticated: false, authTime: null, apiKey: null, user: null });

        // Flush the React-Query cache so the next user doesn't see stale data.
        // Guard against SSR / test environments where the client may not be registered.
        _queryClient?.clear();

        // Reset in-memory zustand stores that hold user-scoped runtime data.
        // Importing lazily avoids the circular-dependency that direct top-level
        // imports would create (those stores don't import auth-store).
        void import('@/stores/chat-store').then(({ useChatStore }) => useChatStore.getState()._reset());
        void import('@/stores/job-store').then(({ useJobStore }) => useJobStore.getState()._reset());
        void import('@/stores/bulk-selection-store').then(({ useBulkSelection }) => useBulkSelection.getState()._reset());
        void import('@/stores/pomodoro-store').then(({ usePomodoroStore }) => usePomodoroStore.getState()._reset());
        void import('@/stores/keyboard-shortcuts-store').then(({ useKeyboardShortcuts }) => useKeyboardShortcuts.getState()._reset());

        // Notify the active Service Worker to drop runtime-API caches so
        // cached responses from the previous user aren't served to the next.
        navigator.serviceWorker?.controller?.postMessage({ type: 'JARVIS_LOGOUT' });

        // Best-effort backend logout: clear the session cookie + revoke the row.
        // Don't await — UI state is already cleared and a network failure
        // shouldn't block the user.
        try {
          void fetch('/api/auth/logout', {
            method: 'POST',
            credentials: 'include',
          });
        } catch {
          // ignore — frontend logout already happened
        }
      },

      checkSession(): boolean {
        const { authTime, isAuthenticated, apiKey, user } = get();
        if (!isAuthenticated || authTime === null) return false;
        // Either an API key OR a session user must be present
        if (!apiKey && !user) return false;
        if (Date.now() - authTime > SESSION_DURATION_MS) {
          set({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
          return false;
        }
        return true;
      },

      getApiKey(): string | null {
        return get().apiKey;
      },

      getUser(): SessionUser | null {
        return get().user;
      },
    }),
    {
      name: 'jarvis-auth',
      // Storage strategy: sessionStorage so credentials are never written to
      // disk and are cleared on tab close. The HttpOnly session cookie is the
      // long-lived credential; this store is just enough to drive UI state
      // for the current tab session.
      storage: createJSONStorage(() => sessionStorage),
      partialize: (state) => ({
        isAuthenticated: state.isAuthenticated,
        authTime: state.authTime,
        apiKey: state.apiKey,
        user: state.user,
      }),
    },
  ),
);
