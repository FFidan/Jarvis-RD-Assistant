import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';
import type { QueryClient } from '@tanstack/react-query';
import { UI_STORE_KEY } from '@/stores/ui-store';
import { abortAllStreams } from '@/stores/chat-store';
import { clearPersistedQueryCache } from '@/lib/query-persister';
import { clearReviewOutbox } from '@/lib/review-outbox';

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
 * 1. Magic-link sessions: the backend issues a session cookie
 *    after /api/auth/verify; the cookie is HttpOnly so the browser can't read
 *    it. This store carries only the user record (id/email/role) returned by
 *    /api/auth/verify so the UI can render greetings + role-gated chrome. The
 *    cookie travels on every fetch automatically (credentials:'include' on
 *    apiFetch).
 *
 * 2. API-key sessions (legacy): users sitting in front of the wizard or
 *    self-hosters who haven't set up SMTP can still paste their JARVIS_API_KEY
 *    into the login form. The key is stored in sessionStorage and threaded
 *    through every fetch as X-API-Key. This path is kept working so
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
  lastError: string | null;
  login: (apiKey: string) => Promise<boolean>;
  loginWithSession: (user: SessionUser) => void;
  logout: () => void;
  /**
   * Pure predicate — reads state only, never mutates.
   * Safe to call during React render (no "update during render" risk).
   * Returns true iff the session exists and has not expired client-side.
   */
  isSessionValid: () => boolean;
  /**
   * Side-effectful clear — call from a useEffect (never during render).
   * Clears the expired session state so the guard redirects to /login on
   * the next render cycle.
   */
  expireSession: () => void;
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
      lastError: null,

      async login(apiKey: string): Promise<boolean> {
        // A valid JARVIS_API_KEY mints a real
        // owner-scoped jarvis_session cookie. We must use credentials:'include'
        // so the Set-Cookie sticks, then store the returned owner user via the
        // SAME path magic-link uses (loginWithSession) so user-data routes
        // (current_user_id_strict) resolve an identity instead of 401-bouncing.
        try {
          const res = await fetch('/api/auth/api-key-session', {
            method: 'POST',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              'X-API-Key': apiKey,
            },
            body: JSON.stringify({ api_key: apiKey }),
          });
          if (res.ok) {
            const user = (await res.json()) as SessionUser;
            get().loginWithSession(user);
            return true;
          }
          // Surface the backend error (esp. the 403 multi-tenant-disabled
          // message) instead of a silent bounce to magic-link.
          let message = 'API-key login failed';
          try {
            const data = (await res.json()) as { detail?: string };
            if (data?.detail) message = data.detail;
          } catch {
            // non-JSON body — keep the generic message
          }
          set({ lastError: message });
          return false;
        } catch {
          set({ lastError: 'Network error during API-key login' });
          return false;
        }
      },

      loginWithSession(user: SessionUser): void {
        // Magic-link path: the session cookie is already set by the backend.
        // We persist nothing security-sensitive here — just the user record so
        // the UI can render. The cookie is HttpOnly so the browser/JS can't
        // read or forge it; isSessionValid() still gates on authTime so a stale
        // tab doesn't pretend to be logged in forever after the cookie
        // expires server-side.
        set({
          isAuthenticated: true,
          authTime: Date.now(),
          apiKey: null,
          user,
          lastError: null,
        });
      },

      logout() {
        // Abort any in-flight SSE streams before clearing session state.
        abortAllStreams();

        // Clear the UI store's persisted localStorage entry so a fresh login
        // doesn't inherit stale UI state from a previous session.
        localStorage.removeItem(UI_STORE_KEY);

        // Clear auth state first.
        set({ isAuthenticated: false, authTime: null, apiKey: null, user: null, lastError: null });

        // Flush the React-Query cache so the next user doesn't see stale data.
        // cancelQueries first so in-flight fetches don't repopulate the cache
        // after clear(). Both are best-effort; a failure must not abort logout.
        void (async () => {
          try {
            if (_queryClient != null) {
              await _queryClient.cancelQueries();
              _queryClient.clear();
            }
          } catch {
            // ignore — cache flush is best-effort
          }

          // Reset in-memory zustand stores that hold user-scoped runtime data.
          // Importing lazily avoids the circular-dependency that direct top-level
          // imports would create (those stores don't import auth-store).
          // Promise.allSettled ensures a failed chunk load for one store does not
          // prevent the others from resetting.
          await Promise.allSettled([
            import('@/stores/chat-store').then(({ useChatStore }) => useChatStore.getState()._reset()).catch((e: unknown) => { console.warn('[auth] chat-store reset failed', e); }),
            import('@/stores/job-store').then(({ useJobStore }) => useJobStore.getState()._reset()).catch((e: unknown) => { console.warn('[auth] job-store reset failed', e); }),
            import('@/stores/bulk-selection-store').then(({ useBulkSelection }) => useBulkSelection.getState()._reset()).catch((e: unknown) => { console.warn('[auth] bulk-selection-store reset failed', e); }),
            import('@/stores/pomodoro-store').then(({ usePomodoroStore }) => usePomodoroStore.getState()._reset()).catch((e: unknown) => { console.warn('[auth] pomodoro-store reset failed', e); }),
            import('@/stores/keyboard-shortcuts-store').then(({ useKeyboardShortcuts }) => useKeyboardShortcuts.getState()._reset()).catch((e: unknown) => { console.warn('[auth] keyboard-shortcuts-store reset failed', e); }),
          ]);
        })();

        // Purge the IndexedDB-persisted TanStack Query cache (cross-user hygiene —
        // P1b contract; also posts JARVIS_LOGOUT to SW for runtime-cache purge).
        // Non-blocking: a storage failure must not block the logout flow.
        // The existing SW postMessage below is kept for defense-in-depth (no-op
        // when clearPersistedQueryCache() already posted it).
        void clearPersistedQueryCache().catch((e: unknown) => {
          console.warn('[auth] IDB cache purge failed', e);
        });

        // FE-A (belt-and-suspenders): wipe the offline review outbox too.
        // review-outbox already isolates per user via purgeForeignEntries; this
        // is additive — a clean slate that also closes the null-user edge where
        // no identity was resolvable at enqueue time. Non-blocking + non-throwing.
        void clearReviewOutbox().catch((e: unknown) => {
          console.warn('[auth] review outbox purge failed', e);
        });

        // Notify the active Service Worker to drop runtime-API caches so
        // cached responses from the previous user aren't served to the next.
        // FE-B: if no SW controls this page yet (logout immediately after the
        // very first install — controller is still null until the new SW
        // claims), register a one-shot controllerchange listener so the purge
        // still fires the moment the SW takes control.
        const sw = navigator.serviceWorker;
        if (sw?.controller) {
          sw.controller.postMessage({ type: 'JARVIS_LOGOUT' });
        } else if (sw) {
          const onControllerChange = (): void => {
            sw.removeEventListener('controllerchange', onControllerChange);
            sw.controller?.postMessage({ type: 'JARVIS_LOGOUT' });
          };
          sw.addEventListener('controllerchange', onControllerChange);
        }

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

      isSessionValid(): boolean {
        const { authTime, isAuthenticated, apiKey, user } = get();
        if (!isAuthenticated || authTime === null) return false;
        if (!apiKey && !user) return false;
        return Date.now() - authTime <= SESSION_DURATION_MS;
      },

      expireSession(): void {
        set({ isAuthenticated: false, authTime: null, apiKey: null, user: null });
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
        user: state.user,
      }),
    },
  ),
);
