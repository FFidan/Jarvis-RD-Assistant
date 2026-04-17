import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

/**
 * Generate Cards on an unprocessed paper must fail with an actionable
 * error: the backend returns an `action_link` pointing the user back
 * to `?action=process`. The sidebar renders the link so clicking it
 * navigates to the same paper with the Process button pulsed.
 */
test.describe('Generate Cards on unprocessed paper shows action_link @cards @jobs', () => {
  const PAPER_ID = 7;
  const DECK_ID = 1;
  const JOB_ID = 'job-gen-7';

  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);

    // Paper is NOT processed (no chunks, no summary).
    await page.route(`**/api/papers/${PAPER_ID}`, async (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          paper: {
            id: PAPER_ID,
            title: 'Unprocessed Paper',
            authors: ['Doe, J.'],
            abstract: 'Not yet processed.',
            source_type: 'arxiv',
            external_id: '0000.0001',
            url: null,
            published_date: '2024-01-01',
            created_at: new Date().toISOString(),
            citation_count: 0,
            priority_score: 0,
            pdf_path: null,
          },
          summary: null,
          chunks: [],
          user_state: null,
        }),
      });
    });

    await page.route(`**/api/papers/${PAPER_ID}/notes**`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });

    // At least one deck so the "Generate Cards" button is enabled.
    await page.route('**/api/decks**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ id: DECK_ID, name: 'Default Deck' }]),
      });
    });

    // Kicking off the job returns job_id — the sidebar then polls.
    await page.route(`**/api/papers/${PAPER_ID}/cards/generate-job**`, async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ job_id: JOB_ID }),
      });
    });

    // Job row poll returns failed with an action_link back to process.
    await page.route(`**/api/jobs/${JOB_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: JOB_ID,
          kind: 'card.generate',
          status: 'failed',
          progress: 0,
          progress_message: null,
          result: null,
          error: {
            message: 'Paper has not been processed yet. Process it first.',
            action_link: {
              label: 'Go process paper',
              href: `/paper/${PAPER_ID}?action=process`,
            },
          },
          created_at: new Date().toISOString(),
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
        }),
      });
    });
  });

  test('failed job surfaces action_link that routes to ?action=process', async ({ page }) => {
    await page.goto(`/paper/${PAPER_ID}`);
    await page.setViewportSize({ width: 1280, height: 900 });

    await expect(page.getByRole('heading', { name: 'Unprocessed Paper' })).toBeVisible({
      timeout: 10_000,
    });

    // Select the deck, then click Generate Cards.
    await page.getByRole('combobox').filter({ hasText: /select a deck|default deck/i }).first().click();
    await page.getByRole('option', { name: 'Default Deck' }).click();

    const generateBtn = page.getByRole('button', { name: /generate cards/i });
    await expect(generateBtn).toBeEnabled();
    await generateBtn.click();

    // The poll will resolve to failed — error + action_link should render.
    await expect(
      page.getByText(/paper has not been processed/i),
    ).toBeVisible({ timeout: 10_000 });

    const link = page.getByRole('link', { name: /go process paper/i });
    await expect(link).toBeVisible();
    await link.click();

    await expect(page).toHaveURL(/\/paper\/7\?action=process/);

    // The Process PDF button should be rendered (and pulsed, but we don't
    // assert on the animate-pulse class — just on existence).
    await expect(page.getByRole('button', { name: /process pdf/i })).toBeVisible({
      timeout: 10_000,
    });
  });
});
