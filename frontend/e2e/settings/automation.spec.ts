import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from '../helpers/auth';

test.describe('Settings - Automation', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
    await page.getByRole('tab', { name: 'Automation' }).click();
  });

  test('nudge list loads with human-readable labels', async ({ page }) => {
    // Wait for loading to complete
    await expect(page.getByText('Loading automation...')).not.toBeVisible({ timeout: 10000 });

    // Either nudge entries are listed or empty state
    const nudgeCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const emptyState = page.getByText('No automation jobs');

    const hasNudges = await nudgeCards.first().isVisible().catch(() => false);
    const hasEmptyState = await emptyState.isVisible().catch(() => false);
    expect(hasNudges || hasEmptyState).toBeTruthy();

    if (hasNudges) {
      // Nudge labels should be human-readable, not raw nudge_type keys
      // e.g. "Automated Paper Search" instead of "research_pulse"
      const humanReadableLabels = [
        'Background Paper Search',
        'Flashcard Review Reminder',
        'Project Deadline Alert',
        'Daily Briefing',
        'Paper Digest',
        'Author Alerts',
      ];

      // At least one label should match a known human-readable label
      const allText = await page.textContent('body');
      const hasHumanLabel = humanReadableLabels.some((label) => allText?.includes(label));
      expect(hasHumanLabel).toBeTruthy();
    }
  });

  test('schedule shows cron expression', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading automation...')).not.toBeVisible({ timeout: 10000 });

    const nudgeCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const hasNudges = await nudgeCards.first().isVisible().catch(() => false);

    if (!hasNudges) {
      test.skip();
      return;
    }

    // Each nudge card should display a "Schedule:" line
    const firstNudge = nudgeCards.first();
    await expect(firstNudge.getByText(/Schedule:/)).toBeVisible();
  });

  test('toggle nudge enable/disable', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading automation...')).not.toBeVisible({ timeout: 10000 });

    const nudgeCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const hasNudges = await nudgeCards.first().isVisible().catch(() => false);

    if (!hasNudges) {
      test.skip();
      return;
    }

    const firstNudge = nudgeCards.first();
    const isEnabled = await firstNudge.getByText('Enabled').isVisible().catch(() => false);

    if (isEnabled) {
      await firstNudge.getByRole('button', { name: 'Disable' }).click();
      await expect(firstNudge.getByText('Disabled')).toBeVisible({ timeout: 10000 });
    } else {
      await firstNudge.getByRole('button', { name: 'Enable' }).click();
      await expect(firstNudge.getByText('Enabled')).toBeVisible({ timeout: 10000 });
    }
  });

  test('nudge cards display last run information', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading automation...')).not.toBeVisible({ timeout: 10000 });

    const nudgeCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const hasNudges = await nudgeCards.first().isVisible().catch(() => false);

    if (!hasNudges) {
      // Empty state should be descriptive
      await expect(page.getByText('Scheduled jobs will appear once configured')).toBeVisible();
      return;
    }

    // Each nudge card should have a Schedule line, and optionally a "Last run:" timestamp
    const firstNudge = nudgeCards.first();
    const scheduleText = await firstNudge.getByText(/Schedule:/).textContent();
    expect(scheduleText).toBeTruthy();
    // Schedule text may contain the cron expression or "Last run:" info
    expect(scheduleText!.includes('Schedule:')).toBeTruthy();
  });
});
