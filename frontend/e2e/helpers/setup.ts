import type { APIRequestContext, Page } from '@playwright/test';

/**
 * Helpers for driving the first-run setup wizard in e2e tests.
 *
 * The backend exposes `PUT /api/config/setup.completed` (no auth required
 * in DEV_MODE; requires X-API-Key otherwise). We flip this flag directly
 * via the APIRequestContext so tests start from a known state and can
 * restore it in `afterEach`.
 *
 * The frontend also requires a logged-in auth-store session before
 * `SetupGate` renders. `seedAuthedSession` writes the persisted Zustand
 * state (`localStorage["jarvis-auth"]`) before the first page load,
 * bypassing the login form entirely — reliable regardless of whether
 * DEV_MODE is on or off.
 */

const API_KEY = process.env.JARVIS_API_KEY ?? 'dev';

function authHeaders(): Record<string, string> {
  return {
    'X-API-Key': API_KEY,
    'Content-Type': 'application/json',
  };
}

export async function forceSetupIncomplete(request: APIRequestContext): Promise<void> {
  const res = await request.put('/api/config/setup.completed', {
    headers: authHeaders(),
    data: { key: 'setup.completed', value: false },
  });
  if (!res.ok()) {
    throw new Error(
      `forceSetupIncomplete failed: ${res.status()} ${await res.text().catch(() => '')}`,
    );
  }
}

export async function markSetupComplete(request: APIRequestContext): Promise<void> {
  const res = await request.put('/api/config/setup.completed', {
    headers: authHeaders(),
    data: { key: 'setup.completed', value: true },
  });
  if (!res.ok()) {
    throw new Error(
      `markSetupComplete failed: ${res.status()} ${await res.text().catch(() => '')}`,
    );
  }
}

/**
 * Seed a valid auth-store session into localStorage before the first
 * navigation. Must be called BEFORE `page.goto(...)` so the store is
 * hydrated by the time `<App/>` renders.
 */
export async function seedAuthedSession(page: Page): Promise<void> {
  const apiKey = API_KEY;
  await page.addInitScript((key: string) => {
    const state = {
      state: {
        isAuthenticated: true,
        authTime: Date.now(),
        apiKey: key,
      },
      version: 0,
    };
    window.localStorage.setItem('jarvis-auth', JSON.stringify(state));
  }, apiKey);
}
