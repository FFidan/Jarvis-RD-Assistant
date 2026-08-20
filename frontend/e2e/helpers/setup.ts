import type { APIRequestContext, Page, Route } from '@playwright/test';

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
 * state (`sessionStorage["jarvis-auth"]`) before the first page load,
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


function jsonResponse(body: unknown, status = 200) {
  return {
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  };
}

const EMPTY_FEED_COUNTS = {
  inbox: 0,
  library: 0,
  reading_list: 0,
  reading: 0,
  done: 0,
  starred: 0,
  trash: 0,
  active: 0,
  kept: 0,
  all_non_trash: 0,
  by_source: {},
  by_topic: [],
  untagged: 0,
};

const SETUP_STATUS = {
  setup_completed: true,
  models_ready: true,
  models_downloading: [],
  topics_count: 0,
  telegram_configured: false,
  telegram_paired: false,
};

export const NAV_PREFS_KEY = 'jarvis-nav-prefs';
export const ONBOARDING_DISMISSED_KEY = 'jarvis-onboarding-dismissed';
export const RETURNING_USER_PREFERENCES = [
  {
    key: 'ui.appearance',
    value: { theme: 'system', accent: 'ink-blue', type: 'serif-calm', density: 'default' },
  },
  {
    key: 'ui.timer',
    value: { workMinutes: 25, shortBreakMinutes: 5, longBreakMinutes: 15, targetCycles: 4 },
  },
  { key: 'ui.nav_mode', value: 'full' },
] as const;

const preferenceState = new WeakMap<Page, Map<string, unknown>>();
const preferenceRoutesInstalled = new WeakSet<Page>();

function preferencesFor(page: Page): Map<string, unknown> {
  let preferences = preferenceState.get(page);
  if (preferences === undefined) {
    preferences = new Map(RETURNING_USER_PREFERENCES.map(({ key, value }) => [key, value]));
    preferenceState.set(page, preferences);
  }
  return preferences;
}

async function fulfillPreferenceRequest(route: Route, page: Page): Promise<boolean> {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  const method = request.method();
  const preferences = preferencesFor(page);

  if (method === 'GET' && path === '/api/config') {
    await route.fulfill(
      jsonResponse(Array.from(preferences, ([key, value]) => ({ key, value }))),
    );
    return true;
  }

  const key = path.startsWith('/api/config/') ? decodeURIComponent(path.slice(12)) : null;
  if (key?.startsWith('ui.') && method === 'PUT') {
    const body = request.postDataJSON() as { value?: unknown } | null;
    if (body === null || !Object.hasOwn(body, 'value')) {
      await route.fulfill(jsonResponse({ detail: 'Missing preference value' }, 400));
      return true;
    }
    preferences.set(key, body.value);
    await route.fulfill(jsonResponse({ key, value: body.value }));
    return true;
  }

  if (key?.startsWith('ui.') && method === 'GET') {
    if (!preferences.has(key)) {
      await route.fulfill(jsonResponse({ detail: 'Preference not found' }, 404));
      return true;
    }
    await route.fulfill(jsonResponse({ key, value: preferences.get(key) }));
    return true;
  }

  return false;
}

async function ensurePreferenceRoutes(page: Page): Promise<void> {
  if (preferenceRoutesInstalled.has(page)) return;
  preferenceRoutesInstalled.add(page);
  await page.route(/\/api\/config(?:\/ui\.(?:appearance|timer|nav_mode))?(?:\?|$)/, async (route) => {
    if (!(await fulfillPreferenceRequest(route, page))) await route.fallback();
  });
}

/**
 * Seed the shell state a returning researcher has: the grouped ("full") nav
 * through the account endpoint and a dismissed onboarding tour.
 *
 * The product default is the short `simple` rail for everyone, so specs that
 * assert group labels or non-essential destinations must opt into the grouped
 * nav; and the tour would otherwise cover the app for a zero-paper account.
 * `installMockedApiDefaults` applies this, so every mocked spec starts here.
 */
export async function seedReturningUserShell(page: Page): Promise<void> {
  preferencesFor(page).set('ui.nav_mode', 'full');
  await ensurePreferenceRoutes(page);
  await page.addInitScript(
    (tourKey) => {
      window.localStorage.setItem(tourKey, 'true');
    },
    ONBOARDING_DISMISSED_KEY,
  );
}

/**
 * Undo `seedReturningUserShell` for the specs that exercise first use itself:
 * no stored nav preference (so the simple rail applies) and no dismissed tour.
 * Init scripts run in registration order, so call this after the defaults.
 */
export async function seedFirstRunShell(page: Page): Promise<void> {
  preferencesFor(page).delete('ui.nav_mode');
  await ensurePreferenceRoutes(page);
  await page.addInitScript(
    ([navKey, tourKey]) => {
      window.localStorage.removeItem(navKey);
      window.localStorage.removeItem(tourKey);
    },
    [NAV_PREFS_KEY, ONBOARDING_DISMISSED_KEY] as const,
  );
}

/**
 * Register common mocked API defaults for app-shell/background requests.
 *
 * Register this before spec-specific routes. Playwright runs matching handlers
 * in reverse registration order, so later per-spec routes override these
 * defaults. Any other /api/** request returns 501 to make missing mocks obvious.
 */
export async function installMockedApiDefaults(page: Page): Promise<void> {
  await seedReturningUserShell(page);

  await page.route(/^https?:\/\/[^/]+\/health\/(paper_ingestion|learning_engine)(?:\/|\?|$)/, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/internal')) {
      await route.fulfill(
        jsonResponse({
          status: 'ok',
          checks: {
            postgres: 'ok',
            qdrant: 'ok',
            litellm: 'ok',
            ollama: 'ok',
            vector: 'ok',
          },
        }),
      );
      return;
    }
    await route.fulfill(jsonResponse({ status: 'ok' }));
  });

  await page.route(/^https?:\/\/[^/]+\/api(?:\/|\?|$)/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (await fulfillPreferenceRequest(route, page)) return;

    if (method === 'GET' && path === '/api/setup/status') {
      await route.fulfill(jsonResponse({ configured: true, setup_completed: true }));
      return;
    }
    if (method === 'GET' && path === '/api/system/setup-status') {
      await route.fulfill(jsonResponse(SETUP_STATUS));
      return;
    }
    if (method === 'GET' && path === '/api/auth/verify') {
      await route.fulfill(jsonResponse({ id: 1, email: 'test@example.com', role: 'user' }));
      return;
    }
    if (method === 'GET' && path === '/api/logs/summary') {
      await route.fulfill(jsonResponse({ by_level: {}, by_category: {}, total: 0 }));
      return;
    }
    if (method === 'GET' && path === '/api/jobs') {
      await route.fulfill(jsonResponse([]));
      return;
    }
    if (method === 'GET' && path === '/api/topics') {
      await route.fulfill(jsonResponse([]));
      return;
    }
    if (method === 'GET' && path === '/api/papers/feed/counts') {
      await route.fulfill(jsonResponse(EMPTY_FEED_COUNTS));
      return;
    }
    if (method === 'GET' && path === '/api/papers/feed') {
      await route.fulfill(jsonResponse({ papers: [], total: 0 }));
      return;
    }
    if (method === 'GET' && /^\/api\/papers\/\d+\/highlights$/.test(path)) {
      await route.fulfill(jsonResponse([]));
      return;
    }
    if (method === 'GET' && /^\/api\/pdfs\/\d+$/.test(path)) {
      await route.fulfill(jsonResponse({ detail: 'PDF not available in mocked e2e' }, 404));
      return;
    }
    if (method === 'GET' && path.startsWith('/api/config/')) {
      await route.fulfill(jsonResponse({ value: true }));
      return;
    }
    if (method === 'GET' && path === '/api/health/stack') {
      await route.fulfill(jsonResponse({ overall: 'ok', services: [], degradedCount: 0, downCount: 0 }));
      return;
    }

    await route.fulfill(
      jsonResponse(
        { error: `Unexpected mocked API request: ${method} ${path}${url.search}` },
        501,
      ),
    );
  });
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
 * Seed a valid auth-store session into sessionStorage before the first
 * navigation. Must be called BEFORE `page.goto(...)` so the store is
 * hydrated by the time `<App/>` renders.
 */
export async function seedAuthedSession(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const state = {
      state: {
        isAuthenticated: true,
        authTime: Date.now(),
        user: { id: 1, email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    };
    window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
  });
  await seedReturningUserShell(page);
}
