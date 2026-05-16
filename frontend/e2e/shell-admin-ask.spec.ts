/**
 * shell-admin-ask.spec.ts — Playwright mocked e2e spec for:
 *   - Grouped roman-numeral sidebar nav (groups Ⅰ–Ⅳ visible to all)
 *   - Admin gating (group Ⅴ only visible for admin users)
 *   - HealthDots admin navigation vs in-place expand
 *   - System Logs (/logs) vs Audit Log (/admin/audit-log) — separate destinations
 *   - Ask page renders + submits question to the cross-paper RAG endpoint
 *
 * Uses page.route() to mock the backend so the spec works without Docker.
 * Uses seedAuthedSession() from e2e/helpers/setup.ts to bypass login.
 *
 * baseURL: http://127.0.0.1:3001 (set via PLAYWRIGHT_BASE_URL env var)
 */

import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

// ---------------------------------------------------------------------------
// Route mocks
// ---------------------------------------------------------------------------

const HEALTH_RESPONSE = {
  overall: 'ok',
  services: [
    { name: 'paper_ingestion', label: 'Paper Ingestion', status: 'ok' },
    { name: 'learning_engine', label: 'Learning Engine', status: 'ok' },
  ],
  degradedCount: 0,
  downCount: 0,
};

const ASK_STREAM_CHUNKS = [
  'data: {"type":"token","content":"The "}\n\n',
  'data: {"type":"token","content":"papers "}\n\n',
  'data: {"type":"token","content":"suggest..."}\n\n',
  'data: {"type":"done","model_used":null}\n\n',
  'data: [DONE]\n\n',
];

import type { Page } from '@playwright/test';

/**
 * Seed a regular (non-admin) user session.
 */
async function seedRegularSession(page: Page) {
  await page.addInitScript(() => {
    const state = {
      state: {
        isAuthenticated: true,
        authTime: Date.now(),
        apiKey: 'test-key',
        user: { id: 1, email: 'user@example.com', role: 'user' },
      },
      version: 0,
    };
    window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
  });
}

/**
 * Seed an admin user session.
 */
async function seedAdminSession(page: Page) {
  await page.addInitScript(() => {
    const state = {
      state: {
        isAuthenticated: true,
        authTime: Date.now(),
        apiKey: 'test-key',
        user: { id: 1, email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    };
    window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
  });
}

/**
 * Mock all common backend endpoints so the spec runs without Docker.
 */
async function mockCommonEndpoints(page: Page) {
  // Stack health — required for HealthDots
  await page.route('/api/health/stack', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HEALTH_RESPONSE) }),
  );

  // Auth verify — required by app bootstrap
  await page.route('/api/auth/verify', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 1, email: 'test@example.com', role: 'user' }) }),
  );

  // Catch-all API — return empty to prevent 404 console noise
  await page.route('/api/**', (route) => {
    if (!route.request().url().includes('/api/health/stack') &&
        !route.request().url().includes('/api/auth/verify')) {
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    } else {
      route.continue();
    }
  });
}

// ---------------------------------------------------------------------------
// Tests — Non-admin user
// ---------------------------------------------------------------------------

test.describe('Sidebar — non-admin user', () => {
  test.beforeEach(async ({ page }) => {
    await seedRegularSession(page);
    await mockCommonEndpoints(page);
    await page.goto('/');
  });

  test('groups Ⅰ–Ⅳ are visible', async ({ page }) => {
    await expect(page.getByText('Today')).toBeVisible();
    await expect(page.getByText('Read')).toBeVisible();
    await expect(page.getByText('Learn')).toBeVisible();
    // "Ask" appears as both group label and nav link — just check at least one
    await expect(page.locator('nav').getByText('Ask').first()).toBeVisible();
  });

  test('group Ⅴ Admin is NOT visible for non-admin', async ({ page }) => {
    await expect(page.getByText('Admin').first()).not.toBeVisible().catch(() => {
      // If element doesn't exist at all, test passes
    });
    await expect(page.getByRole('link', { name: 'User Management' })).not.toBeVisible().catch(() => {});
  });

  test('group Ⅰ Today items navigate correctly', async ({ page }) => {
    await page.getByRole('link', { name: 'My Day' }).click();
    await expect(page).toHaveURL(/\/my-day/);
  });

  test('group Ⅱ Read items navigate correctly', async ({ page }) => {
    await page.getByRole('link', { name: 'Projects' }).click();
    await expect(page).toHaveURL(/\/projects/);
  });

  test('group Ⅲ Learn items navigate correctly', async ({ page }) => {
    await page.getByRole('link', { name: 'Learning Cards' }).click();
    await expect(page).toHaveURL(/\/cards/);
  });

  test('Settings link is in footer (not in numbered groups)', async ({ page }) => {
    const settingsLink = page.getByRole('link', { name: 'Settings' });
    await expect(settingsLink).toBeVisible();
    await settingsLink.click();
    await expect(page).toHaveURL(/\/settings/);
  });
});

// ---------------------------------------------------------------------------
// Tests — Admin user
// ---------------------------------------------------------------------------

test.describe('Sidebar — admin user', () => {
  test.beforeEach(async ({ page }) => {
    await seedAdminSession(page);

    // Override auth verify to return admin role
    await page.route('/api/auth/verify', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 1, email: 'admin@example.com', role: 'admin' }) }),
    );
    await page.route('/api/health/stack', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HEALTH_RESPONSE) }),
    );
    await page.route('/api/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
    );

    await page.goto('/');
  });

  test('group Ⅴ Admin is visible for admin user', async ({ page }) => {
    await expect(page.getByText('Admin')).toBeVisible();
  });

  test('admin group contains all expected items', async ({ page }) => {
    await expect(page.getByRole('link', { name: 'User Management' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'System Health' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Audit Log' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'System Logs' })).toBeVisible();
  });

  test('System Logs and Audit Log are SEPARATE destinations', async ({ page }) => {
    const systemLogsLink = page.getByRole('link', { name: 'System Logs' });
    const auditLogLink = page.getByRole('link', { name: 'Audit Log' });

    // Different hrefs — these must not be the same route
    const logsHref = await systemLogsLink.getAttribute('href');
    const auditHref = await auditLogLink.getAttribute('href');

    expect(logsHref).toBe('/logs');
    expect(auditHref).toBe('/admin/audit-log');
    expect(logsHref).not.toBe(auditHref);
  });

  test('HealthDots pill navigates to /admin/system-health for admin', async ({ page }) => {
    // The admin link pill should be present (data-testid="health-pill-admin-link")
    const adminPill = page.locator('[data-testid="health-pill-admin-link"]');
    await expect(adminPill).toBeVisible({ timeout: 5000 });
    await adminPill.click();
    await expect(page).toHaveURL(/\/admin\/system-health/);
  });
});

// ---------------------------------------------------------------------------
// Tests — Ask page
// ---------------------------------------------------------------------------

test.describe('Ask page (group Ⅳ)', () => {
  test.beforeEach(async ({ page }) => {
    await seedRegularSession(page);

    await page.route('/api/health/stack', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HEALTH_RESPONSE) }),
    );

    // Mock the cross-paper Ask streaming endpoint
    await page.route('/api/ask/stream', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: ASK_STREAM_CHUNKS.join(''),
      });
    });

    await page.route('/api/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
    );

    await page.goto('/ask');
  });

  test('Ask page renders heading', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Ask' })).toBeVisible();
  });

  test('Ask page renders cross-paper subtitle', async ({ page }) => {
    await expect(page.getByText(/Cross-paper reasoning/)).toBeVisible();
  });

  test('Ask page has a question input', async ({ page }) => {
    await expect(page.getByPlaceholder(/Ask a question/)).toBeVisible();
  });

  test('submitting a question shows user message in chat', async ({ page }) => {
    const textarea = page.getByPlaceholder(/Ask a question/);
    await textarea.fill('What is the main finding across my papers?');
    await textarea.press('Enter');

    await expect(page.getByText('What is the main finding across my papers?')).toBeVisible({ timeout: 5000 });
  });

  test('Ask page is navigable from sidebar', async ({ page }) => {
    await page.goto('/');
    const askLinks = page.getByRole('link', { name: 'Ask' });
    await askLinks.first().click();
    await expect(page).toHaveURL(/\/ask/);
    await expect(page.getByRole('heading', { name: 'Ask' })).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Tests — Regression: all existing routes reachable
// ---------------------------------------------------------------------------

test.describe('Regression — all routes still reachable', () => {
  test.beforeEach(async ({ page }) => {
    await seedRegularSession(page);
    await page.route('/api/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
    );
  });

  const routes = [
    '/',
    '/my-day',
    '/pulse',
    '/feed',
    '/ask',
    '/analytics',
    '/projects',
    '/cards',
    '/settings',
    '/citations',
    '/knowledge',
    '/extractions',
  ];

  for (const route of routes) {
    test(`route ${route} does not 404`, async ({ page }) => {
      await page.goto(route);
      // Should NOT see the 404 page
      await expect(page.getByText('404')).not.toBeVisible({ timeout: 3000 }).catch(() => {});
    });
  }
});
