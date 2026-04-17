import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

/**
 * Settings → Sources drag-and-drop reordering.
 *
 * Verifies that swapping the first two source cards persists the new
 * order across a page reload. We mock the list and the reorder
 * endpoint so the spec is deterministic — the second fetch returns
 * the reordered list.
 */
test.describe('Settings Sources reorder persists @sources', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);

    const initial = [
      {
        source_type: 'arxiv',
        enabled: true,
        priority: 1,
        config: {},
      },
      {
        source_type: 'semantic_scholar',
        enabled: true,
        priority: 2,
        config: {},
      },
      {
        source_type: 'openalex',
        enabled: true,
        priority: 3,
        config: {},
      },
    ];

    const reordered = [initial[1], initial[0], initial[2]];
    let served = initial;

    await page.route('**/api/config/sources', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(served),
        });
        return;
      }
      return route.continue();
    });

    // POST /api/config/sources/reorder — mutates the served order.
    await page.route('**/api/config/sources/reorder', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      served = reordered;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      });
    });
  });

  test('drag-and-drop reorders sources and persists across reload', async ({ page }) => {
    await page.goto('/settings');
    await page.getByRole('tab', { name: 'Sources' }).click();

    // Wait for the three source cards to render.
    const handles = page.getByRole('button', { name: 'Drag to reorder' });
    await expect(handles).toHaveCount(3, { timeout: 10_000 });

    // Drag the first handle (arxiv) down past the second (semantic_scholar).
    const first = handles.nth(0);
    const second = handles.nth(1);
    await first.hover();
    await page.mouse.down();
    const secondBox = await second.boundingBox();
    if (secondBox) {
      await page.mouse.move(secondBox.x + secondBox.width / 2, secondBox.y + secondBox.height + 10, {
        steps: 10,
      });
    }
    await page.mouse.up();

    // After the reorder POST, served === reordered. Reload and re-check
    // that the #1 position is now Semantic Scholar.
    await page.reload();
    await page.getByRole('tab', { name: 'Sources' }).click();

    const sourceNames = page.locator('.font-medium.capitalize');
    await expect(sourceNames.first()).toHaveText(/semantic_scholar|semantic scholar/i, {
      timeout: 10_000,
    });
  });
});
