import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';
import type { QueryClient } from '@tanstack/react-query';
import { UI_STORE_KEY, useUIStore } from '@/stores/ui-store';
import { usePomodoroStore } from '@/stores/pomodoro-store';
import { runSessionResets } from '@/stores/session-reset';
import { clearPersistedQueryCache } from '@/lib/query-persister';
import { clearReviewOutbox } from '@/lib/review-outbox';
import { decodeResponseJson } from '@/lib/api/core';
import { sessionUserSchema } from '@/lib/api/schemas/auth';
import { apiErrorDetailSchema } from '@/lib/api/schemas/common';

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
 *    the shared API client).
 *
 * 2. API-key login: users at the wizard or self-hosters without
 *    SMTP can paste their JARVIS_API_KEY into the login form. login() exchanges
 *    it server-side for a real session cookie (POST /api/auth/api-key-session)
 *    and then takes the SAME path magic-link uses (loginWithSession), so the
 *    raw key is never held in the store and the HttpOnly cookie is the credential.
 *
 * The raw key is sent only to the exchange endpoint and is not retained by the
 * store or reused by the shared API clients.
 */
// 30 days — mirrors jarvis_common/session_middleware.py SESSION_TTL. The backend
// 401 interceptor (core.ts handleAuthFailure → logout) remains the authoritative
// fast-path for real expiry/revocation.
export const SESSION_DURATION_MS = 30 * 24 * 60 * 60 * 1000;

export interface SessionUser {
  id: number;
  email: string;
  role: 'user' | 'admin';
}

interface AuthState {
  isAuthenticated: boolean;
  authTime: number | null;
  user: SessionUser | null;
  lastError: string | null;
  login: (apiKey: string) => Promise<boolean>;
  loginWithSession: (user: SessionUser) => Promise<void>;
  /**
   * Restore the CURRENT identity from a still-valid session cookie (e.g. a new
   * tab where sessionStorage is empty). Unlike loginWithSession this never
   * purges caches — it restores an identity, it does not switch users.
   */
  hydrateFromCookie: (user: SessionUser) => void;
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
  getUser: () => SessionUser | null;
}

function safe(label: string, fn: () => void): void {
  try {
    fn();
  } catch (e) {
    console.warn(`[auth] ${label} failed`, e);
  }
}

/** Fire-and-forget JARVIS_LOGOUT to the controlling SW, deferring via a one-shot
 *  controllerchange listener when no SW controls the page yet. Used by paths
 *  that do NOT expose a new identity (logout / session expiry). */
function notifySwLogout(): void {
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
}

/** Post JARVIS_LOGOUT with a reply port and resolve once the SW acknowledges its
 *  runtime API cache is cleared — so the next identity is never served the prior
 *  user's SW-cached data. Resolves immediately when no SW controls the page
 *  (nothing can be served stale) and after `timeoutMs` if the SW never replies
 *  (never block login on an unresponsive SW). */
function awaitSwApiCacheCleared(timeoutMs = 1500): Promise<void> {
  const controller = navigator.serviceWorker?.controller;
  if (!controller) return Promise.resolve();
  return new Promise<void>((resolve) => {
    const channel = new MessageChannel();
    let settled = false;
    const finish = (): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, timeoutMs);
    channel.port1.onmessage = finish;
    try {
      controller.postMessage({ type: 'JARVIS_LOGOUT' }, [channel.port2]);
    } catch {
      finish();
    }
  });
}

/** Resolve when `p` settles or after `timeoutMs`, whichever comes first — so an
 *  IndexedDB transaction that never settles (e.g. another tab holds the DB open)
 *  can't block login indefinitely. Mirrors awaitSwApiCacheCleared's bound. */
function withTimeout(p: Promise<unknown>, timeoutMs: number): Promise<void> {
  return new Promise<void>((resolve) => {
    let settled = false;
    const finish = (): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, timeoutMs);
    void Promise.resolve(p).then(finish, finish);
  });
}

/**
 * Cross-user cache hygiene — the in-app half of the logout fan-out, also
 * invoked on session expiry (client timeout / tab-close) and on re-login. Drops
 * every trace of the current identity that lives inside the app: persisted UI
 * flags, the React-Query cache (in-memory + IndexedDB-persisted), in-memory
 * user-scoped stores, and the offline review outbox. The service worker's
 * runtime API cache is cleared separately by the caller (notifySwLogout on
 * logout/expiry, awaitSwApiCacheCleared on re-login). Without this, the next
 * user on a shared browser would render the previous user's private data.
 * Every step is best-effort: a storage or network failure must never block the
 * auth flow.
 */
async function purgeIdentityCaches(): Promise<void> {
  safe('ui-store key purge', () => localStorage.removeItem(UI_STORE_KEY));

  // Clear the prior identity's in-memory React-Query cache synchronously, before
  // the caller exposes a new identity — NOT behind an awaited cancel, which
  // would leave PREV's cache live for the microtask between set(NEXT) and the
  // cancel resolving. cancelQueries is fire-and-forget; clear() empties the
  // cache synchronously, and after set(NEXT) the prior observers unmount so a
  // late in-flight fetch can't re-populate. Best-effort.
  safe('query cache flush', () => {
    if (_queryClient != null) {
      void _queryClient.cancelQueries().catch(() => {});
      _queryClient.clear();
    }
  });

  // Registry-routed so auth-store forms no import cycle with the user-scoped
  // stores (job-store reads auth state); chat-store's reset aborts its SSE
  // streams. pomodoro + ui are leaf stores, reset directly. Synchronous,
  // before the caller exposes a new identity.
  safe('session resets', runSessionResets);
  safe('pomodoro reset', () => usePomodoroStore.getState()._reset());
  safe('ui reset', () => useUIStore.getState()._reset());

  // Both IDB calls are INITIATED synchronously (before the await) so callers
  // that use fire-and-forget (void purgeIdentityCaches()) still observe them
  // synchronously. The await lets loginWithSession guarantee both are complete
  // before exposing the new identity, bounded by a timeout so a stuck IDB
  // transaction can never block login. logout/expireSession don't await the
  // returned Promise so the IDB ops are fire-and-forget there.
  await withTimeout(
    Promise.all([
      clearPersistedQueryCache().catch((e: unknown) => {
        console.warn('[auth] IDB cache purge failed', e);
      }),
      // Closes the null-user edge where no identity was resolvable at enqueue time.
      clearReviewOutbox().catch((e: unknown) => {
        console.warn('[auth] review outbox purge failed', e);
      }),
    ]),
    2000,
  );
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      isAuthenticated: false,
      authTime: null,
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
            try {
              const user = await decodeResponseJson(
                res,
                '/api/auth/api-key-session',
                sessionUserSchema,
              );
              await get().loginWithSession(user);
              return true;
            } catch {
              set({ lastError: 'API-key login returned an invalid response' });
              return false;
            }
          }
          // Surface the backend error (esp. the 403 multi-tenant-disabled
          // message) instead of a silent bounce to magic-link.
          let message = 'API-key login failed';
          try {
            const data = await decodeResponseJson(
              res,
              '/api/auth/api-key-session',
              apiErrorDetailSchema,
            );
            if (typeof data.detail === 'string') message = data.detail;
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

      async loginWithSession(user: SessionUser): Promise<void> {
        // Magic-link path: the session cookie is already set by the backend.
        // We persist nothing security-sensitive here — just the user record so
        // the UI can render. The cookie is HttpOnly so the browser/JS can't
        // read or forge it; isSessionValid() still gates on authTime so a stale
        // tab doesn't pretend to be logged in forever after the cookie
        // expires server-side.
        //
        // Purge the previous identity's caches BEFORE exposing the new user.
        // The in-memory clears run synchronously; the IDB snapshot delete and SW
        // runtime-API cache clear are both AWAITED so a re-login on a shared
        // browser can never rehydrate the prior user's persisted or SW-cached
        // private data.
        await purgeIdentityCaches();
        await awaitSwApiCacheCleared();
        set({
          isAuthenticated: true,
          authTime: Date.now(),
          user,
          lastError: null,
        });
      },

      hydrateFromCookie(user: SessionUser): void {
        set({
          isAuthenticated: true,
          authTime: Date.now(),
          user,
          lastError: null,
        });
      },

      logout() {
        // Clear auth state first so the UI reflects logged-out immediately.
        set({ isAuthenticated: false, authTime: null, user: null, lastError: null });

        void purgeIdentityCaches();
        notifySwLogout();

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
        const { authTime, isAuthenticated, user } = get();
        if (!isAuthenticated || authTime === null) return false;
        if (!user) return false;
        return Date.now() - authTime <= SESSION_DURATION_MS;
      },

      expireSession(): void {
        set({ isAuthenticated: false, authTime: null, user: null });
        // Client timeout / tab-close on a shared browser: purge the expired
        // identity's caches so the next user never inherits its persisted or
        // SW-cached private data.
        void purgeIdentityCaches();
        notifySwLogout();
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
