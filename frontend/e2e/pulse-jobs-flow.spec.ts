import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

/**
 * Pulse → Jobs integration.
 *
 * Starts a `pulse.generate` job from My Day, verifies the TopBar
 * JobsIndicator surfaces a running row, and confirms the
 * PulsePreviewCard refreshes once the job succeeds.
 *
 * This is a mocked-stack spec — the backend routes for
 *   POST /api/jobs
 *   GET  /api/jobs/stream
 *   GET  /api/pulse/today
 * are stubbed so the flow is deterministic. Run against a live stack
 * too if desired; the assertions are written to be tolerant.
 */
test.describe('Pulse jobs flow @pulse @jobs', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);

    // Mock initial pulse/today as empty so we have a "generate now" affordance.
    let pulseDeck: unknown = null;

    await page.route('**/api/pulse/today', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(pulseDeck),
      });
    });

    // Job creation: return a predictable id.
    await page.route('**/api/jobs', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          job_id: 'job-pulse-1',
          status: 'queued',
          kind: 'pulse.generate',
          progress: 0,
        }),
      });
    });

    // Job list hydrate on mount: return one running job so the
    // JobsIndicator is forced visible regardless of SSE connectivity.
    await page.route('**/api/jobs?**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'job-pulse-1',
            kind: 'pulse.generate',
            status: 'running',
            progress: 0.4,
            progress_message: 'Scoring candidate papers…',
            result: null,
            error: null,
            created_at: new Date().toISOString(),
            started_at: new Date().toISOString(),
            finished_at: null,
          },
        ]),
      });
    });

    // Pulse SSE stream — return an immediate "done" event so the job
    // transitions to succeeded. The store will then invalidate the
    // pulse-today query; flip the mock state so the refetch returns
    // a populated deck.
    await page.route('**/api/jobs/**/stream', async (route) => {
      pulseDeck = {
        deck_id: 42,
        date: new Date().toISOString().slice(0, 10),
        generated_at: new Date().toISOString(),
        cards: [
          {
            card_id: 100,
            paper_id: 1,
            title: 'Mocked Pulse Paper',
            why: 'Matches topic "transformers"',
            topic_badges: ['transformers'],
            novelty_score: 0.8,
            score: 0.9,
          },
        ],
        card_count: 1,
        degraded_reason: null,
      };
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body:
          `event: status\ndata: {"status":"succeeded","progress":1.0}\n\n` +
          `event: done\ndata: {}\n\n`,
      });
    });
  });

  test('generate pulse surfaces a running job and then a populated deck', async ({ page }) => {
    await page.goto('/my-day');

    // Kick off generation — button label changes between states.
    const generateBtn = page.getByRole('button', { name: /generate pulse now|refresh pulse/i });
    await expect(generateBtn).toBeVisible({ timeout: 10_000 });
    await generateBtn.click();

    // The JobsIndicator becomes visible once a job lands in the store.
    const jobsButton = page.getByRole('button', { name: /background jobs/i });
    await expect(jobsButton).toBeVisible({ timeout: 10_000 });

    // Open the popover and assert a "Generating Pulse" row is listed.
    await jobsButton.click();
    await expect(page.getByText(/generating pulse/i)).toBeVisible({ timeout: 5_000 });

    // After the SSE stream completes and pulse-today refetches, the
    // preview card should render the mocked paper.
    await expect(page.getByText('Mocked Pulse Paper')).toBeVisible({ timeout: 10_000 });
  });
});
