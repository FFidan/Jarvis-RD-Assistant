import { useAuthStore } from '@/stores/auth-store';

/** Canonical unauthenticated state for frontend tests. */
export const LOGGED_OUT_AUTH = {
  isAuthenticated: false,
  authTime: null,
  user: null,
  lastError: null,
} satisfies Partial<ReturnType<typeof useAuthStore.getState>>;

/** Restore the auth store to its unauthenticated state. */
export function resetAuthState(): void {
  useAuthStore.setState(LOGGED_OUT_AUTH);
}
