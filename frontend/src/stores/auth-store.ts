import { create } from 'zustand';
import { persist } from 'zustand/middleware';

/**
 * Real API-key-based authentication.
 *
 * The user enters their JARVIS_API_KEY to log in. The key is validated
 * against the backend (GET /api/topics) and stored locally so every
 * subsequent fetch includes the X-API-Key header.
 *
 * nginx does NOT inject the API key — the browser must send it.
 */
const SESSION_DURATION_MS = 8 * 60 * 60 * 1000; // 8 hours

interface AuthState {
  isAuthenticated: boolean;
  authTime: number | null;
  apiKey: string | null;
  login: (apiKey: string) => Promise<boolean>;
  logout: () => void;
  checkSession: () => boolean;
  getApiKey: () => string | null;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      isAuthenticated: false,
      authTime: null,
      apiKey: null,

      async login(apiKey: string): Promise<boolean> {
        try {
          // Validate the API key against the backend
          const res = await fetch('/api/topics', {
            headers: { 'X-API-Key': apiKey },
          });
          if (res.ok || res.status === 200) {
            set({ isAuthenticated: true, authTime: Date.now(), apiKey });
            return true;
          }
          return false;
        } catch {
          return false;
        }
      },

      logout() {
        set({ isAuthenticated: false, authTime: null, apiKey: null });
      },

      checkSession(): boolean {
        const { authTime, isAuthenticated, apiKey } = get();
        if (!isAuthenticated || authTime === null || !apiKey) return false;
        if (Date.now() - authTime > SESSION_DURATION_MS) {
          set({ isAuthenticated: false, authTime: null, apiKey: null });
          return false;
        }
        return true;
      },

      getApiKey(): string | null {
        return get().apiKey;
      },
    }),
    {
      name: 'jarvis-auth',
      partialize: (state) => ({
        isAuthenticated: state.isAuthenticated,
        authTime: state.authTime,
        apiKey: state.apiKey,
      }),
    },
  ),
);
