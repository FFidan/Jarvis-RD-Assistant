import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

/**
 * Generate Cards on an unprocessed paper (no chunks) must be disabled so
 * the user cannot trigger card generation on a paper that hasn't been
 * processed yet. The tooltip already explains the requirement.
 */
test.describe('Generate Cards on unprocessed paper is disabled @cards @jobs', () => {
  const PAPER_ID = 7;
  const DECK_ID = 1;

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

    // The Generate Cards button will be disabled (no chunks) — no POST to /api/generate
    // should be made, but we stub it defensively to catch regressions.
    await page.route(`**/api/generate**`, async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      // Button is disabled — this route should never be hit.
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Paper not processed — button should have been disabled' }),
      });
    });
  });

  test('Generate Cards button is disabled when paper has no chunks', async ({ page }) => {
    await page.goto(`/paper/${PAPER_ID}`);
    await page.setViewportSize({ width: 1280, height: 900 });

    await expect(page.getByRole('heading', { name: 'Unprocessed Paper' })).toBeVisible({
      timeout: 10_000,
    });

    // Select a deck — even with a deck selected, the button must stay disabled
    // because the paper has no chunks (chunks: [] in the mock above).
    await page.getByRole('combobox').filter({ hasText: /select a deck|default deck/i }).first().click();
    await page.getByRole('option', { name: 'Default Deck' }).click();

    const generateBtn = page.getByRole('button', { name: /generate cards/i });
    await expect(generateBtn).toBeDisabled();

    // The Process PDF button should be available for the user to act on.
    await expect(page.getByRole('button', { name: /process pdf/i })).toBeVisible({
      timeout: 10_000,
    });
  });
});
