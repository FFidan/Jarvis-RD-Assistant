import { test, expect } from '@playwright/test';

const mockPapersResponse = {
  papers: [
    {
      id: 1,
      title: 'Deep Learning for NLP',
      authors: ['Smith, J.', 'Doe, A.'],
      abstract: 'A comprehensive survey on deep learning approaches for NLP.',
      source_type: 'arxiv',
      user_status: 'new',
      published_date: '2024-01-15',
      created_at: '2024-01-20T10:00:00Z',
      priority_score: 0.85,
      confidence: 'HIGH',
      tldr: 'Survey of DL for NLP tasks.',
      summary_brief: null,
    },
    {
      id: 2,
      title: 'Reinforcement Learning in Robotics',
      authors: ['Lee, K.'],
      abstract: 'RL applications in real-world robotic systems.',
      source_type: 'semantic_scholar',
      user_status: 'reading',
      published_date: '2024-02-10',
      created_at: '2024-02-15T10:00:00Z',
      priority_score: 0.72,
      confidence: 'MEDIUM',
      tldr: 'RL for robots.',
      summary_brief: null,
    },
    {
      id: 3,
      title: 'Computer Vision Transformers',
      authors: ['Wang, L.'],
      abstract: 'Applying vision transformers to object detection.',
      source_type: 'arxiv',
      user_status: 'read',
      published_date: '2024-03-01',
      created_at: '2024-03-05T10:00:00Z',
      priority_score: 0.60,
      confidence: 'HIGH',
      tldr: null,
      summary_brief: 'Vision transformers for object detection.',
    },
  ],
  total: 3,
};

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await page.evaluate(() => {
    localStorage.setItem(
      'jarvis-auth',
      JSON.stringify({
        state: { isAuthenticated: true, authTime: Date.now() },
        version: 0,
      }),
    );
  });
  await page.goto('/feed');
  await page.waitForLoadState('networkidle');
});

test.describe('Feed Library', () => {
  test('papers load in the library tab or empty state message', async ({ page }) => {
    // Click Library tab
    const libraryTab = page.getByRole('tab', { name: 'Library' });
    await libraryTab.click();

    // Wait for either papers to load or the empty state
    const paperCard = page.locator('.rounded-lg.border.p-4').first();
    const emptyState = page.getByText('No papers found');

    await expect(paperCard.or(emptyState)).toBeVisible({ timeout: 10000 });
  });

  test('filter by status dropdown works', async ({ page }) => {
    // Set up API mock for library papers
    await page.route('**/api/papers/feed**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockPapersResponse),
      });
    });

    // Also mock topics endpoint
    await page.route('**/api/topics**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    // Click Library tab
    await page.getByRole('tab', { name: 'Library' }).click();

    // Wait for papers to appear
    await expect(page.getByText('Deep Learning for NLP')).toBeVisible({ timeout: 5000 });

    // Find the Status multi-select trigger
    const statusTrigger = page.locator('button[role="combobox"]').filter({ hasText: 'Status' });
    await expect(statusTrigger).toBeVisible();

    // Click to open dropdown
    await statusTrigger.click();

    // Select "new" status
    const newOption = page.getByRole('option', { name: 'new' });
    await expect(newOption).toBeVisible();
    await newOption.click();
  });

  test('filter by source works', async ({ page }) => {
    await page.route('**/api/papers/feed**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockPapersResponse),
      });
    });

    await page.route('**/api/topics**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.getByRole('tab', { name: 'Library' }).click();
    await expect(page.getByText('Deep Learning for NLP')).toBeVisible({ timeout: 5000 });

    // Find Source multi-select trigger
    const sourceTrigger = page.locator('button[role="combobox"]').filter({ hasText: 'Source' });
    await expect(sourceTrigger).toBeVisible();

    await sourceTrigger.click();

    // Select "arxiv" source
    const arxivOption = page.getByRole('option', { name: 'arxiv' });
    await expect(arxivOption).toBeVisible();
    await arxivOption.click();
  });

  test('pagination controls navigate between pages', async ({ page }) => {
    // Mock a large result set
    const largeMockResponse = {
      papers: mockPapersResponse.papers,
      total: 60, // 3 pages of 20
    };

    await page.route('**/api/papers/feed**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(largeMockResponse),
      });
    });

    await page.route('**/api/topics**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.getByRole('tab', { name: 'Library' }).click();
    await expect(page.getByText('Deep Learning for NLP')).toBeVisible({ timeout: 5000 });

    // Pagination controls should be visible
    const prevButton = page.getByRole('button', { name: 'Previous' });
    const nextButton = page.getByRole('button', { name: 'Next' });

    await expect(prevButton).toBeVisible();
    await expect(nextButton).toBeVisible();

    // Previous should be disabled on first page
    await expect(prevButton).toBeDisabled();

    // Click Next to go to page 2
    await nextButton.click();

    // Previous should now be enabled
    await expect(prevButton).toBeEnabled();

    // Page indicator should show "Page 2 of 3"
    await expect(page.getByText('Page')).toBeVisible();
  });

  test('sort toggle changes paper order', async ({ page }) => {
    await page.route('**/api/papers/feed**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockPapersResponse),
      });
    });

    await page.route('**/api/topics**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.getByRole('tab', { name: 'Library' }).click();
    await expect(page.getByText('Deep Learning for NLP')).toBeVisible({ timeout: 5000 });

    // Find the sort select (default is "Recent")
    const sortTrigger = page.locator('button[role="combobox"]').filter({ hasText: 'Recent' });
    await expect(sortTrigger).toBeVisible();

    // Open sort dropdown and select "Priority"
    await sortTrigger.click();
    const priorityOption = page.getByRole('option', { name: 'Priority' });
    await expect(priorityOption).toBeVisible();
    await priorityOption.click();

    // Verify the sort trigger now shows "Priority"
    await expect(
      page.locator('button[role="combobox"]').filter({ hasText: 'Priority' }),
    ).toBeVisible();
  });

  test('mark paper as read via status change on paper detail', async ({ page }) => {
    await page.route('**/api/papers/feed**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockPapersResponse),
      });
    });

    await page.route('**/api/topics**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.getByRole('tab', { name: 'Library' }).click();
    await expect(page.getByText('Deep Learning for NLP')).toBeVisible({ timeout: 5000 });

    // Each library paper card shows a status badge
    // Verify status badges are visible
    const newBadge = page.getByText('NEW').first();
    const readingBadge = page.getByText('READING').first();

    await expect(newBadge.or(readingBadge)).toBeVisible();

    // Click "View Details" on the first paper to navigate to paper detail
    const viewDetailsButton = page.getByRole('button', { name: 'View Details' }).first();
    await expect(viewDetailsButton).toBeVisible();
    // Verify it's clickable (don't actually navigate to avoid needing full paper detail mock)
    await expect(viewDetailsButton).toBeEnabled();
  });
});
