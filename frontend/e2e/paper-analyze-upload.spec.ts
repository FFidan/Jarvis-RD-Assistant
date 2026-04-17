import path from 'node:path';
import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

/**
 * PDF upload → paper.analyze job flow.
 *
 * Uploads a sample PDF from `fixtures/sample.pdf`, intercepts the
 * upload + process endpoints, and asserts the UI transitions through
 * Uploading → Indexing → Done while the JobsIndicator reflects
 * progress. After completion we open the freshly-created paper and
 * confirm chunks/summary mocks render.
 */
test.describe('PDF upload triggers paper.analyze job @jobs', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);

    // Mock uploadPdf (POST /api/papers/upload-pdf)
    await page.route('**/api/papers/upload-pdf', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 999,
          title: 'sample',
          source_type: 'upload',
          external_id: 'upload-999',
          authors: [],
          abstract: null,
          url: null,
        }),
      });
    });

    // Mock processPdf (POST /api/papers/{id}/process)
    await page.route('**/api/papers/999/process', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ paper_id: 999, chunk_count: 12 }),
      });
    });

    // Hydrate JobsIndicator with a running analyze job so the popover shows it.
    await page.route('**/api/jobs?**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'job-analyze-999',
            kind: 'paper.analyze',
            status: 'running',
            progress: 0.5,
            progress_message: 'Chunking & embedding…',
            result: null,
            error: null,
            created_at: new Date().toISOString(),
            started_at: new Date().toISOString(),
            finished_at: null,
          },
        ]),
      });
    });

    // Paper detail mock so navigating to /paper/999 works after upload.
    await page.route('**/api/papers/999', async (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          paper: {
            id: 999,
            title: 'sample',
            authors: [],
            abstract: 'Uploaded sample.',
            source_type: 'upload',
            external_id: 'upload-999',
            url: null,
            published_date: null,
            created_at: new Date().toISOString(),
            citation_count: 0,
            priority_score: 0,
            pdf_path: '/data/pdfs/sample.pdf',
          },
          summary: {
            id: 1,
            paper_id: 999,
            summary_brief: 'Brief summary.',
            summary_detailed: 'Detailed.',
            methodology: null,
            limitations: null,
            key_findings: [],
            confidence: 'MEDIUM',
            summary_verified: true,
          },
          chunks: [
            { id: 1, paper_id: 999, chunk_index: 0, content: 'First chunk.', page_number: 1 },
          ],
          user_state: null,
        }),
      });
    });

    await page.route('**/api/papers/999/notes**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/decks**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
  });

  test('upload sample.pdf → job visible → navigate to processed paper', async ({ page }) => {
    await page.goto('/feed?tab=new');

    // The PdfUploadZone sits inside the New tab. Grab the hidden file input.
    // The zone attaches a `<input type="file" accept=".pdf" multiple>` element.
    const fileInput = page.locator('input[type="file"][accept=".pdf"]').first();
    await expect(fileInput).toBeAttached({ timeout: 10_000 });

    const fixturePath = path.join(__dirname, 'fixtures', 'sample.pdf');
    await fileInput.setInputFiles(fixturePath);

    // Status row should render for the file.
    await expect(page.getByText(/sample\.pdf/i)).toBeVisible({ timeout: 5_000 });
    await expect(
      page.getByText(/uploading|indexing|done/i).first(),
    ).toBeVisible({ timeout: 10_000 });

    // JobsIndicator should now be visible (seeded by the /api/jobs hydrate mock).
    const jobsButton = page.getByRole('button', { name: /background jobs/i });
    await expect(jobsButton).toBeVisible({ timeout: 10_000 });
    await jobsButton.click();
    await expect(page.getByText(/analyzing paper/i)).toBeVisible({ timeout: 5_000 });

    // Navigate to the uploaded paper; chunks + summary mocks should render.
    await page.goto('/paper/999');
    await expect(page.getByRole('heading', { name: 'sample' })).toBeVisible({ timeout: 10_000 });
    await page.getByRole('tab', { name: 'Chunks' }).click();
    await expect(page.getByText(/first chunk/i)).toBeVisible({ timeout: 5_000 });
  });
});
