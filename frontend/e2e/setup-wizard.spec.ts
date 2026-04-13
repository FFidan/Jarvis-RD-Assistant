/**
 * Setup wizard happy path + skip flow (C1).
 *
 * Drives the first-run wizard via real API calls against the running
 * backend (`/api/system/setup-status`, `PUT /api/config/setup.completed`).
 *
 * Live backend smoke (manual, run from project root before merging):
 *   docker compose down -v && docker compose up -d
 *   docker compose exec postgres psql -U jarvis -d jarvis -c "\dt telegram_pairing"
 *   curl -k -H "X-API-Key: $JARVIS_API_KEY" https://localhost:3001/api/system/setup-status | jq .
 *   curl -k -X POST -H "X-API-Key: $JARVIS_API_KEY" https://localhost:3001/api/telegram/pairing | jq .
 */

import { test, expect } from '@playwright/test';
import {
  forceSetupIncomplete,
  markSetupComplete,
  seedAuthedSession,
} from './helpers/setup';

test.describe('Setup wizard', () => {
  test.beforeEach(async ({ page, request }) => {
    await forceSetupIncomplete(request);
    await seedAuthedSession(page);
  });

  test.afterEach(async ({ request }) => {
    // Always restore the flag so downstream tests see a completed setup.
    await markSetupComplete(request);
  });

  test('redirects to /setup when setup_completed is false', async ({ page }) => {
    await page.goto('/');

    await page.waitForURL(/\/setup\?step=1/, { timeout: 15_000 });
    await expect(
      page.getByRole('heading', { name: /welcome to jarvis/i }),
    ).toBeVisible();
    await expect(page.getByText(/step 1 of 6/i)).toBeVisible();
  });

  test('Skip setup button from step 1 marks setup completed and lands on /', async ({
    page,
  }) => {
    await page.goto('/setup?step=1');
    await expect(
      page.getByRole('heading', { name: /welcome to jarvis/i }),
    ).toBeVisible();

    // Wait for the PUT to land so the refetch of setup-status returns true.
    const markResponse = page.waitForResponse(
      (resp) =>
        resp.url().includes('/api/config/setup.completed') &&
        resp.request().method() === 'PUT' &&
        resp.status() === 200,
    );
    await page.getByRole('button', { name: /skip setup/i }).click();
    await markResponse;

    // A hard reload drops any stale `setup-status` cache in React Query so
    // `SetupGate` reads the freshly-persisted `setup.completed=true` on mount.
    // Without this, a transient refetch race can bounce the user back to
    // /setup before the new value lands in the cache.
    await page.goto('/');
    await expect(page).toHaveURL(/\/$/);
    await expect(
      page.getByRole('heading', { name: 'Dashboard' }).first(),
    ).toBeVisible();

    // Reload again — should still NOT redirect back.
    await page.reload();
    await expect(page).toHaveURL(/\/$/);
    await expect(
      page.getByRole('heading', { name: 'Dashboard' }).first(),
    ).toBeVisible();
  });

  test('navigates through wizard steps via Next / Skip buttons', async ({ page }) => {
    await page.goto('/setup?step=1');
    await expect(page.getByText(/step 1 of 6/i)).toBeVisible();

    // Step 1 → 2 (Get started)
    await page.getByRole('button', { name: /get started/i }).click();
    await expect(page).toHaveURL(/step=2/);
    await expect(page.getByRole('heading', { name: /system check/i })).toBeVisible();

    // Step 2 → 3 (Next is always enabled)
    await page.getByRole('button', { name: /^next$/i }).click();
    await expect(page).toHaveURL(/step=3/);
    await expect(
      page.getByRole('heading', { name: /your first research topic/i }),
    ).toBeVisible();

    // Step 3 → 4 (no topic added yet → button text is "Skip for now")
    await page.getByRole('button', { name: /skip for now/i }).click();
    await expect(page).toHaveURL(/step=4/);
    await expect(page.getByRole('heading', { name: /automation schedule/i })).toBeVisible();

    // Step 4 → 5 ("Skip for now" until a schedule is saved)
    await page.getByRole('button', { name: /skip for now/i }).click();
    await expect(page).toHaveURL(/step=5/);
    await expect(page.getByRole('heading', { name: /pair telegram/i })).toBeVisible();

    // Step 5 → 6
    await page.getByRole('button', { name: /skip for now/i }).click();
    await expect(page).toHaveURL(/step=6/);
    await expect(page.getByRole('heading', { name: /you're all set/i })).toBeVisible();

    // Step 6 → dashboard. DoneStep's effect marks setup.completed=true; then
    // the Go to dashboard button navigates to "/". We reload once so React
    // Query refetches setup-status and SetupGate sees the fresh value.
    await page.getByRole('button', { name: /go to dashboard/i }).click();
    await page.goto('/');
    await expect(page).toHaveURL(/\/$/);
    await expect(
      page.getByRole('heading', { name: 'Dashboard' }).first(),
    ).toBeVisible();
  });
});
