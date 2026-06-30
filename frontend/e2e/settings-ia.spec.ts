/**
 * Settings IA redesign — 2-pane navigator (mocked).
 *
 * Verifies:
 *  - Rail renders §I–§VI sections.
 *  - §II Sources rail items appear from mocked GET /api/sources.
 *  - §IV System hidden for non-admin session.
 *  - Rail item click updates breadcrumb and detail pane heading.
 *  - ?confirm_email_token query param → AccountSection calls confirm endpoint
 *    and shows success banner.
 *  - Default landing is Research / Topics.
 *
 * Uses page.route to mock API responses; no live backend required.
 * seedAuthedSession writes sessionStorage before first navigation.
 */
import { test, expect, type Page } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

const MOCK_ACCOUNT = {
  id: 1,
  email: 'test@example.com',
  role: 'admin',
  display_name: 'Ada Test',
  created_at: '2025-01-15T10:00:00Z',
  last_login_at: '2026-05-15T08:30:00Z',
};

const MOCK_SOURCES = [
  { id: 1, source_type: 'arxiv', enabled: true, priority: 1, config: {}, display_order: 0, created_at: '' },
  { id: 2, source_type: 'semantic_scholar', enabled: true, priority: 2, config: {}, display_order: 1, created_at: '' },
  { id: 3, source_type: 'openalex', enabled: false, priority: 3, config: {}, display_order: 2, created_at: '' },
];

const MOCK_SETUP_STATUS = {
  setup_completed: true,
  models_ready: true,
  models_downloading: [],
  topics_count: 0,
  telegram_configured: false,
  telegram_paired: false,
};

// ---------------------------------------------------------------------------
// Common setup: seed session + mock API routes
// ---------------------------------------------------------------------------

async function seedAdminSession(page: Page) {
  const apiKey = process.env.JARVIS_API_KEY ?? 'dev';
  await page.addInitScript((key: string) => {
    const state = {
      state: {
        isAuthenticated: true,
        authTime: Date.now(),
        apiKey: key,
        user: { id: 1, email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    };
    window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
  }, apiKey);
}

async function setupMocks(page: Page) {
  await seedAdminSession(page);

  // SetupGate uses GET /api/system/setup-status (not /api/setup/status).
  // Mock both so neither gate redirects.
  await page.route('**/api/system/setup-status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MOCK_SETUP_STATUS),
    });
  });

  // FirstRunGate uses GET /api/setup/status (pre-auth, first-run).
  await page.route('**/api/setup/status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ configured: true, setup_completed: true }),
    });
  });

  // Mock GET /api/account and PATCH
  await page.route('**/api/account/confirm-email', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...MOCK_ACCOUNT, email: 'confirmed@example.com' }),
    });
  });

  await page.route('**/api/account', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_ACCOUNT),
      });
    } else if (route.request().method() === 'PATCH') {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      if (body.email) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ account: MOCK_ACCOUNT, email_verification_sent: true }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            account: { ...MOCK_ACCOUNT, display_name: body.display_name },
            email_verification_sent: false,
          }),
        });
      }
    } else {
      await route.continue();
    }
  });

  // Mock GET /api/sources
  await page.route('**/api/sources', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_SOURCES),
      });
    } else {
      await route.continue();
    }
  });

  // Stub config endpoint
  await page.route('**/api/config', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    } else {
      await route.continue();
    }
  });

  // Stub topics
  await page.route('**/api/topics', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });

  await page.route('**/api/topic-subscriptions', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });

  await page.route('**/api/authors/tracked', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Settings IA 2-pane navigation @settings-ia', () => {
  test('default landing is Research / Topics', async ({ page }) => {
    await setupMocks(page);
    await page.goto('/settings');

    // h2 detail pane heading should say "Topics"
    await expect(page.getByRole('heading', { name: 'Topics', level: 2 })).toBeVisible({ timeout: 8000 });
    // Breadcrumb should show Topics
    await expect(page.getByRole('navigation', { name: 'breadcrumb' })).toContainText('Topics');
  });

  test('Account section header appears in rail', async ({ page }) => {
    await setupMocks(page);
    await page.goto('/settings');
    await expect(page.getByText('Account', { exact: true })).toBeVisible({ timeout: 8000 });
  });

  test('Sources rail item appears (single Sources entry)', async ({ page }) => {
    await setupMocks(page);
    await page.goto('/settings');

    // Wait for rail to render — admin session so Sources should appear.
    // Sources is now a single "Sources" rail item (SettingsRail ALL_SECTIONS).
    await expect(page.getByText('Sources', { exact: true }).first()).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('button', { name: 'Sources' })).toBeVisible({ timeout: 8000 });
  });

  test('clicking §I Account / Profile & Email shows Account detail pane', async ({ page }) => {
    await setupMocks(page);
    await page.goto('/settings');

    await page.getByRole('button', { name: 'Profile & Email' }).click();

    // Detail pane heading
    await expect(page.getByRole('heading', { name: 'Profile & Email', level: 2 })).toBeVisible({ timeout: 8000 });
    // Breadcrumb updated
    await expect(page.getByRole('navigation', { name: 'breadcrumb' })).toContainText('Account');
    // AccountSection renders profile data
    await expect(page.getByTestId('display-name-value')).toContainText('Ada Test');
    await expect(page.getByTestId('email-value')).toContainText('test@example.com');
  });

  test('non-admin session sees §I, §V, §VI but not §II, §III, §IV', async ({ page }) => {
    // Seed as non-admin (role=user)
    await page.addInitScript(() => {
      const state = {
        state: {
          isAuthenticated: true,
          authTime: Date.now(),
          apiKey: 'dev',
          user: { id: 2, email: 'user@example.com', role: 'user' },
        },
        version: 0,
      };
      window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
    });

    await page.route('**/api/system/setup-status', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SETUP_STATUS) });
    });
    await page.route('**/api/setup/status', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) });
    });
    await page.route('**/api/account', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...MOCK_ACCOUNT, role: 'user' }) });
    });
    await page.route('**/api/sources', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/config', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/topics', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/topic-subscriptions', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/authors/tracked', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });

    await page.goto('/settings');
    const nav = page.getByRole('navigation', { name: 'Settings navigation' });
    await expect(nav.getByText('Account', { exact: true })).toBeVisible({ timeout: 8000 });
    await expect(nav.getByText('Integrations', { exact: true })).toBeVisible();
    await expect(nav.getByText('Research', { exact: true })).toBeVisible();

    await expect(page.getByText('Sources', { exact: true })).toHaveCount(0);
    await expect(page.getByText('Models', { exact: true })).toHaveCount(0);
    await expect(page.getByText('System', { exact: true })).toHaveCount(0);
  });

  test('non-admin deep-link to sources redirects to Topics', async ({ page }) => {
    await page.addInitScript(() => {
      const state = {
        state: {
          isAuthenticated: true,
          authTime: Date.now(),
          apiKey: 'dev',
          user: { id: 2, email: 'user@example.com', role: 'user' },
        },
        version: 0,
      };
      window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
    });
    await page.route('**/api/system/setup-status', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SETUP_STATUS) });
    });
    await page.route('**/api/setup/status', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) });
    });
    await page.route('**/api/sources', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/config', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/topics', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/topic-subscriptions', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/authors/tracked', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });

    await page.goto('/settings?section=sources&item=arxiv');
    // Should show Topics (default personal — non-admin can't access sources)
    await expect(page.getByRole('heading', { name: 'Topics', level: 2 })).toBeVisible({ timeout: 8000 });
  });

  test('?confirm_email_token param triggers confirm flow and shows success', async ({ page }) => {
    await setupMocks(page);
    await page.goto('/settings?section=account&item=profile&confirm_email_token=tok-abc-123');

    // AccountSection should show success banner
    await expect(page.getByText(/Email address updated to/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(/confirmed@example.com/i)).toBeVisible();

    // Token should be stripped from URL
    await expect(page).not.toHaveURL(/confirm_email_token/);
  });

  test('display_name edit flow works in Account pane', async ({ page }) => {
    await setupMocks(page);
    await page.goto('/settings?section=account&item=profile');

    // Wait for profile to load
    await expect(page.getByTestId('display-name-value')).toBeVisible({ timeout: 8000 });

    // Click edit
    await page.getByRole('button', { name: /edit display name/i }).click();
    const input = page.getByRole('textbox', { name: /display name/i });
    await expect(input).toBeVisible();

    await input.fill('Grace Hopper');
    await page.getByRole('button', { name: /save display name/i }).click();

    // Mock returns updated name
    await expect(page.getByTestId('display-name-value')).toContainText('Grace Hopper', { timeout: 5000 });
  });
});
