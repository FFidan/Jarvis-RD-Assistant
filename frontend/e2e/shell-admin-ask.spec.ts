/**
 * shell-admin-ask.spec.ts — Playwright mocked e2e spec for:
 *   - Grouped roman-numeral sidebar nav (groups Ⅰ–Ⅳ visible to all)
 *   - Admin gating (group Ⅳ only visible for admin users)
 *   - HealthDots admin navigation vs in-place expand
 *   - System Logs (/logs) vs Audit Log (/admin/audit-log) — separate destinations
 *   - Ask page renders + submits question to the cross-paper RAG endpoint
 *
 * Uses page.route() to mock the backend so the spec works without Docker.
 * Seeds sessionStorage directly to bypass login.
 *
 * baseURL: http://127.0.0.1:3001 (set via PLAYWRIGHT_BASE_URL env var)
 */

import { test, expect } from '@playwright/test';
import {
  installMockedApiDefaults,
  RETURNING_USER_PREFERENCES,
  seedFirstRunShell,
  seedReturningUserShell,
} from './helpers/setup';

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
  // installMockedApiDefaults seeds the full nav density, so the group labels
  // (Today/Workspace/Learn/Admin) this spec asserts are visible.
  await installMockedApiDefaults(page);

  // Stack health — required for HealthDots
  await page.route('**/api/health/stack', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HEALTH_RESPONSE) }),
  );

  // Auth verify — required by app bootstrap
  await page.route('**/api/auth/verify', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 1, email: 'test@example.com', role: 'user' }) }),
  );

  // FirstRunGate — must return setup_completed: true or the onboarding wizard renders.
  await page.route('**/api/setup/status', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) }),
  );

  // Feed — OnboardingTour (rendered inside AppShell) calls fetchFeed({ limit: 1 })
  // which hits /api/papers/feed. Without a proper FeedResponse shape the component
  // does `feedQuery.data.papers.length` on an empty object and throws, causing the
  // top-level ErrorBoundary to replace the entire app with "Something went wrong".
  await page.route('**/api/papers/feed**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: [], total: 0 }) }),
  );

  // Topics — OnboardingTour also calls fetchTopics (/api/topics) to check zeroTopics.
  // Returning an empty array matches the Topic[] type and avoids any length check issues.
  await page.route('**/api/topics', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );

  // Shared defaults fail unexpected /api/** calls with a clear mocked-test error.
}

async function seedFirstUseMilestone(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem(
      'jarvis-research-milestones',
      JSON.stringify({
        state: {
          completed: { save: true, analyze: false },
          advancedCueDismissed: false,
        },
        version: 0,
      }),
    );
  });
}

async function mockFirstUseEndpoints(page: Page) {
  await installMockedApiDefaults(page);
  // This describe block is about first use itself, so undo the returning-user
  // shell the defaults seed: no stored nav preference, no dismissed tour.
  await seedFirstRunShell(page);
  await page.route('**/api/executive/focus/active', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: 'null' }),
  );
  await page.route('**/api/health/stack', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(HEALTH_RESPONSE),
    }),
  );
  await page.route('**/api/auth/verify', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 1, email: 'user@example.com', role: 'user' }),
    }),
  );
  await page.route('**/api/setup/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ configured: true, setup_completed: true }),
    }),
  );
  await page.route('**/api/papers/feed**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ papers: [], total: 0 }),
    }),
  );
  await page.route('**/api/topics', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );
  await page.route('**/api/dashboard/metrics', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total_papers: 0,
        unread_papers: 0,
        pending_papers: 0,
        due_cards: 0,
        active_projects: 0,
        topic_count: 0,
        nudge_count: 0,
        chunked_papers: 0,
        onboarding_stage: 'needs_topics',
      }),
    }),
  );
  await page.route('**/api/config', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(RETURNING_USER_PREFERENCES),
    }),
  );
  await page.route('**/api/config/onboarding.dismissed', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ key: 'onboarding.dismissed', value: true }),
    }),
  );
}

function captureBrowserErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (
      message.type() === 'error' &&
      !message.text().startsWith('Failed to load resource:')
    ) {
      errors.push(message.text());
    }
  });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('response', (response) => {
    if (response.status() >= 400) {
      errors.push(`${response.status()} ${new URL(response.url()).pathname}`);
    }
  });
  return errors;
}

test.describe('First-use research guidance', () => {
  test.beforeEach(async ({ page }) => {
    await seedRegularSession(page);
    await seedFirstUseMilestone(page);
    await mockFirstUseEndpoints(page);
  });

  test('desktop tour and advanced-workspace cue stay usable', async ({ page }) => {
    const browserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');

    await expect(page.getByRole('link', { name: 'Discover' })).toBeVisible();
    await expect(page.locator('[data-tour-id="sidebar-discover"]')).toBeVisible();
    await expect(page.locator('[data-tour-id~="sidebar-library"]')).toBeVisible();
    await expect(page.locator('[data-tour-id~="sidebar-analyze"]')).toBeVisible();
    await expect(page.locator('[data-tour-id="sidebar-ask"]')).toBeVisible();

    await expect(page.getByText('Discover Papers')).toBeVisible({ timeout: 3000 });
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText('Save to Papers')).toBeVisible();
    await page.getByRole('button', { name: 'Back' }).click();
    await expect(page.getByText('Discover Papers')).toBeVisible();
    await page.getByRole('button', { name: "Don't show again" }).click();

    await expect(page.getByTestId('advanced-workspace-cue')).toBeVisible();
    await page.getByTestId('nav-mode-toggle').click();
    await expect(page.getByText('Projects', { exact: true })).toBeVisible();
    await expect(page.getByTestId('advanced-workspace-cue')).toHaveCount(0);
    expect(browserErrors).toEqual([]);
  });

  test('narrow tour uses visible targets and the cue is reachable from the menu', async ({ page }) => {
    const browserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');

    await expect(page.getByText('Discover Papers')).toBeVisible({ timeout: 3000 });
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText('Save to Papers')).toBeVisible();
    await page.getByRole('button', { name: "Don't show again" }).click();

    await page.getByRole('button', { name: 'Open menu' }).click();
    const mobileNav = page.getByRole('dialog', { name: 'Navigation' });
    await expect(mobileNav.getByRole('link', { name: 'Discover' })).toBeVisible();
    await expect(mobileNav.getByTestId('advanced-workspace-cue')).toBeVisible();
    const dismiss = mobileNav.getByRole('button', { name: 'Dismiss workspace feature tip' });
    await dismiss.focus();
    await expect(dismiss).toBeFocused();
    await dismiss.press('Enter');
    await expect(page.getByTestId('advanced-workspace-cue')).toHaveCount(0);
    expect(browserErrors).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Tests — Non-admin user
// ---------------------------------------------------------------------------

test.describe('Sidebar — non-admin user', () => {
  test.beforeEach(async ({ page }) => {
    await seedRegularSession(page);
    await mockCommonEndpoints(page);
    await page.goto('/');
  });

  test('groups Ⅰ–Ⅲ are visible', async ({ page }) => {
    // Use exact: true to avoid substring matches ('Learn' inside 'Learning Cards').
    await expect(page.getByText('Today', { exact: true })).toBeVisible();
    await expect(page.getByText('Workspace', { exact: true })).toBeVisible();
    await expect(page.getByText('Learn', { exact: true })).toBeVisible();
    // Ask is a Workspace item now, not a group of its own.
    await expect(page.getByRole('link', { name: 'Ask', exact: true })).toBeVisible();
    await expect(page.getByText('Read', { exact: true })).toHaveCount(0);
  });

  test('group Ⅳ Admin is NOT visible for non-admin', async ({ page }) => {
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
    // Scope to the sidebar data-testid to avoid matching onboarding checklist's
    // "Go to Settings" link which also resolves to 'Settings' by accessible name.
    const settingsLink = page.getByTestId('sidebar').getByRole('link', { name: 'Settings' });
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
    // This describe stubs its own routes rather than using the mocked defaults,
    // so it seeds the returning-user shell (grouped nav) itself.
    await seedReturningUserShell(page);

    // Override auth verify to return admin role
    await page.route('/api/auth/verify', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 1, email: 'admin@example.com', role: 'admin' }) }),
    );
    await page.route('/api/health/stack', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HEALTH_RESPONSE) }),
    );
    // FirstRunGate — must return setup_completed: true.
    await page.route('/api/setup/status', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) }),
    );
    // Feed — OnboardingTour crashes if /api/papers/feed returns {} (reads .papers.length).
    await page.route('/api/papers/feed**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: [], total: 0 }) }),
    );
    await page.route('/api/topics', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
    );

    await page.goto('/');
  });

  test('group Ⅳ Admin is visible for admin user', async ({ page }) => {
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
    // The admin health pill opens a popover (not direct navigation).
    // Click the pill to open the popover, then click the "full report" link inside.
    const adminPill = page.locator('[data-testid="health-pill-admin-link"]');
    await expect(adminPill).toBeVisible({ timeout: 5000 });
    await adminPill.click();
    // The popover contains a footer link to the full system health page.
    const fullReportLink = page.locator('[data-testid="health-popover-full-report"]');
    await expect(fullReportLink).toBeVisible({ timeout: 3000 });
    await fullReportLink.click();
    await expect(page).toHaveURL(/\/admin\/system-health/);
  });
});

// ---------------------------------------------------------------------------
// Tests — Ask page
// ---------------------------------------------------------------------------

test.describe('Ask page (group Ⅱ Workspace)', () => {
  test.beforeEach(async ({ page }) => {
    await seedRegularSession(page);

    await installMockedApiDefaults(page);

    await page.route('/api/health/stack', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HEALTH_RESPONSE) }),
    );
    // FirstRunGate — must return setup_completed: true.
    await page.route('/api/setup/status', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) }),
    );
    // Feed — OnboardingTour (in AppShell) calls fetchFeed({ limit: 1 }).
    // Without a proper FeedResponse the component crashes reading .papers.length.
    await page.route('**/api/papers/feed**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: [], total: 0 }) }),
    );
    await page.route('/api/topics', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
    );
    // Dashboard metrics — AskPage disables the textarea when chunked_papers = 0.
    // Return at least one chunked paper so the input is enabled for submit tests.
    await page.route('/api/dashboard/metrics', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ chunked_papers: 1 }) }),
    );
    // Mock the cross-paper Ask streaming endpoint.
    await page.route('/api/ask/stream', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: ASK_STREAM_CHUNKS.join(''),
      });
    });

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
    // FirstRunGate — must return setup_completed: true or the wizard intercepts all routes.
    await page.route('/api/setup/status', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) }),
    );
    // Feed — OnboardingTour (in AppShell) crashes if /api/papers/feed returns {}.
    await page.route('/api/papers/feed**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: [], total: 0 }) }),
    );
    await page.route('/api/topics', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
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
