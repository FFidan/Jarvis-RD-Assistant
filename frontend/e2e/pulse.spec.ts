import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from './helpers/auth';

/**
 * Phase 1 Pulse happy-path smoke test.
 *
 * Covers:
 *   1. Enable Pulse in Settings → Automation
 *   2. Trigger a manual deck generation from My Day
 *   3. Rate the first card with thumbs-up and verify POST /api/pulse/rate
 *   4. Switch to Research Feed → Pulse History and confirm the deck appears
 *
 * The test requires a live stack (backend + frontend) reachable at
 * PLAYWRIGHT_BASE_URL. When the stack is down we skip rather than fail so
 * this spec can live alongside unit tests in CI without blocking offline work.
 */
test.describe('Pulse — Phase 1 happy path', () => {
  // Give the full generate → score → persist chain time to run.
  test.setTimeout(120_000);

  test.beforeEach(async ({ page }) => {
    // Skip gracefully if the dashboard is unreachable.
    try {
      const resp = await page.request.get('/', { timeout: 3_000 });
      if (!resp.ok()) {
        test.skip(true, `Dashboard unreachable (status ${resp.status()})`);
      }
    } catch (err) {
      test.skip(true, `Dashboard unreachable: ${(err as Error).message}`);
    }

    await ensureAuthenticated(page);

    // Enable Pulse via Settings → Automation.
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
    await page.getByRole('tab', { name: 'Automation' }).click();

    const pulseToggle = page.getByRole('switch', { name: /enable pulse/i });
    await expect(pulseToggle).toBeVisible({ timeout: 10_000 });
    const isChecked = (await pulseToggle.getAttribute('aria-checked')) === 'true';
    if (!isChecked) {
      await pulseToggle.click();
      // Let the PUT complete before navigating away.
      await expect(pulseToggle).toHaveAttribute('aria-checked', 'true', {
        timeout: 10_000,
      });
    }
  });

  test('user can generate and rate a Pulse deck', async ({ page }) => {
    // Step 1: go to My Day and trigger generation.
    await page.goto('/my-day');

    // If a deck already exists from an earlier run, skip the generate step.
    const existingCards = page.getByTestId('pulse-card');
    const hasDeckAlready = await existingCards
      .first()
      .isVisible({ timeout: 3_000 })
      .catch(() => false);

    if (!hasDeckAlready) {
      const generateBtn = page.getByRole('button', { name: /generate now/i });
      await expect(generateBtn).toBeVisible({ timeout: 10_000 });
      await generateBtn.click();
    }

    // Step 2: wait for at least one card to appear. We don't pin the count
    // to 10 — in a fresh stack with few source papers, the deck may be
    // smaller, and the acceptance target is "a deck exists", not its size.
    const pulseCards = page.getByTestId('pulse-card');
    await expect(pulseCards.first()).toBeVisible({ timeout: 60_000 });

    // Step 3: rate the first card and assert the POST fires.
    const firstCard = pulseCards.first();
    const thumbsUp = firstCard.getByRole('button', { name: /thumbs up/i });
    await expect(thumbsUp).toBeVisible();

    const ratePromise = page.waitForResponse(
      (resp) =>
        resp.url().includes('/api/pulse/rate') && resp.request().method() === 'POST',
      { timeout: 15_000 },
    );
    await thumbsUp.click();
    const rateResp = await ratePromise;
    expect(rateResp.status()).toBeLessThan(400);

    // Step 4: visit Research Feed → Pulse History and confirm the deck is
    // listed. The today's deck entry renders with a date label and a
    // "N papers" summary line.
    await page.goto('/feed');
    await page.getByRole('tab', { name: /pulse history/i }).click();

    // Empty state is acceptable iff no deck exists — but we just made one,
    // so the list should be non-empty.
    await expect(
      page.getByText(/no past pulse decks yet/i),
    ).not.toBeVisible({ timeout: 5_000 });

    // The list item contains a "N papers" summary line — match on that.
    await expect(page.getByText(/\d+ papers · generated/i).first()).toBeVisible({
      timeout: 10_000,
    });
  });
});
