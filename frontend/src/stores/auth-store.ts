import { create } from 'zustand';
import { persist } from 'zustand/middleware';

/**
 * UI-only gate — not a security boundary.
 * Real API authentication is handled by nginx injecting X-API-Key proxy headers.
 * This password only prevents casual access to the dashboard UI.
 */
const DASHBOARD_PASSWORD = import.meta.env.VITE_DASHBOARD_PASSWORD || '';
const SESSION_DURATION_MS = 8 * 60 * 60 * 1000; // 8 hours

interface AuthState {
  isAuthenticated: boolean;
  authTime: number | null;
  login: (password: string) => boolean;
  logout: () => void;
  checkSession: () => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      isAuthenticated: DASHBOARD_PASSWORD === '',
      authTime: DASHBOARD_PASSWORD === '' ? Date.now() : null,

      login(password: string): boolean {
        // Bypass if no password configured
        if (DASHBOARD_PASSWORD === '') {
          set({ isAuthenticated: true, authTime: Date.now() });
          return true;
        }
        // Constant-time comparison to mitigate timing attacks
        const maxLen = Math.max(password.length, DASHBOARD_PASSWORD.length);
        let mismatch = password.length ^ DASHBOARD_PASSWORD.length;
        for (let i = 0; i < maxLen; i++) {
          mismatch |= (password.charCodeAt(i) || 0) ^ (DASHBOARD_PASSWORD.charCodeAt(i) || 0);
        }
        if (mismatch !== 0) {
          return false;
        }
        set({ isAuthenticated: true, authTime: Date.now() });
        return true;
      },

      logout() {
        set({ isAuthenticated: false, authTime: null });
      },

      checkSession(): boolean {
        // If no password configured, always valid
        if (DASHBOARD_PASSWORD === '') return true;

        const { authTime, isAuthenticated } = get();
        if (!isAuthenticated || authTime === null) return false;
        if (Date.now() - authTime > SESSION_DURATION_MS) {
          set({ isAuthenticated: false, authTime: null });
          return false;
        }
        return true;
      },
    }),
    {
      name: 'jarvis-auth',
      partialize: (state) => ({
        isAuthenticated: state.isAuthenticated,
        authTime: state.authTime,
      }),
    },
  ),
);
