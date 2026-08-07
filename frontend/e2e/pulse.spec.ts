import { test, expect, type Page } from '@playwright/test';
import { ensureAuthenticated } from './helpers/auth';

/**
 * Pulse happy-path smoke test (post Round 4 redesign).
 *
 * Round 4 moved Pulse surfaces:
 *   - Enable Pulse toggle lives in Settings → Pulse tab (not Automation)
 *   - My Day renders PulsePreviewCard (top 3 cards + Generate / Refresh button)
 *   - Background generation is tracked via the TopBar JobsIndicator
 *   - Research Feed → Pulse tab shows the full deck + history
 *
 * The test requires a live stack (backend + frontend) reachable at
 * PLAYWRIGHT_BASE_URL, so run it deliberately against a running stack.
 */

// ---------------------------------------------------------------------------
// Reachability guard — an unreachable dashboard is a failure, not a skip
// ---------------------------------------------------------------------------

// Skipping when the target stack is down would let a run report success having
// executed zero tests, so a stack that is not reachable must fail the run
// loudly instead.
async function assertDashboardReachable(page: Page): Promise<void> {
  let resp;
  try {
    resp = await page.request.get('/', { timeout: 3_000 });
  } catch (err) {
    throw new Error(`Dashboard unreachable: ${(err as Error).message}`);
  }
  if (!resp.ok()) {
    throw new Error(`Dashboard unreachable (status ${resp.status()})`);
  }
}

test.describe('Pulse — post Round 4 happy path', () => {
  // Give the full generate → score → persist chain time to run.
  test.setTimeout(120_000);

  test.beforeEach(async ({ page }) => {
    await assertDashboardReachable(page);

    await ensureAuthenticated(page);

    // Enable Pulse via Settings → Pulse tab.
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings' }).first()).toBeVisible();
    await page.getByRole('tab', { name: 'Pulse' }).click();

    const pulseToggle = page.getByRole('switch', { name: /enable pulse/i });
    await expect(pulseToggle).toBeVisible({ timeout: 10_000 });
    const isChecked = (await pulseToggle.getAttribute('aria-checked')) === 'true';
    if (!isChecked) {
      await pulseToggle.click();
      await expect(pulseToggle).toHaveAttribute('aria-checked', 'true', {
        timeout: 10_000,
      });
    }
  });

  test('user can generate Pulse from My Day and rate a card', async ({ page }) => {
    // Step 1: go to My Day — PulsePreviewCard lives directly on the triage layout.
    await page.goto('/my-day');

    // Confirm we're on My Day (DayHeader greeting)
    await expect(
      page.getByRole('heading', { name: /Good (morning|afternoon|evening)/ }),
    ).toBeVisible({ timeout: 10_000 });

    // PulsePreviewCard header contains "Today's Pulse"
    await expect(page.getByText(/Today's Pulse/)).toBeVisible({ timeout: 10_000 });

    // If a deck already exists, pulse cards are already rendered.
    const existingCards = page.getByTestId('pulse-card');
    const hasDeckAlready = await existingCards
      .first()
      .isVisible({ timeout: 3_000 })
      .catch(() => false);

    if (!hasDeckAlready) {
      // Trigger generation — button label is "Generate Pulse now" on empty deck,
      // "Refresh Pulse" on stale deck. Match either.
      const generateBtn = page
        .getByRole('button', { name: /Generate Pulse now|Refresh Pulse/i })
        .first();
      await expect(generateBtn).toBeVisible({ timeout: 10_000 });
      await generateBtn.click();

      // JobsIndicator in the TopBar should now show a running 'pulse.generate' job.
      // The button has aria-label="Background jobs" and is rendered only when jobs exist.
      const jobsButton = page.getByRole('button', { name: 'Background jobs' });
      await expect(jobsButton).toBeVisible({ timeout: 15_000 });
    }

    // Step 2: wait for at least one pulse card to render in the preview.
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

    // Step 4: "View all" link from the preview takes us to Research Feed → Pulse tab.
    const viewAllLink = page.getByRole('link', { name: /View all \d+/ });
    if (await viewAllLink.isVisible().catch(() => false)) {
      await viewAllLink.click();
      await expect(page).toHaveURL(/\/feed\?tab=pulse/);
    }
  });
});
