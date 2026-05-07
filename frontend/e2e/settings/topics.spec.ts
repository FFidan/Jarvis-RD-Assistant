import { test, expect } from '@playwright/test';
import { ensureAuthenticated } from '../helpers/auth';

test.describe('Settings - Topics', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
    await page.getByRole('tab', { name: 'Topics' }).click();
  });

  test('does not render redundant § TOPICS marker (W4-2 regression guard)', async ({ page }) => {
    // Wait for tab content to settle
    await expect(page.getByText('Loading topics...')).not.toBeVisible({ timeout: 10000 });
    // No § TOPICS, § SOURCES, § AUTHORS, § INGESTION, § AUTOMATION, § PULSE,
    // § TIMER, § PROVIDERS, § INTEGRATIONS, § EXTRACTION TEMPLATES, § APPEARANCE.
    // Locator is anchored to the Settings page main; sidebar/MyDay markers
    // (§ Yesterday etc.) are unaffected.
    const main = page.getByRole('main');
    const forbidden = [
        '§ TOPICS', '§ SOURCES', '§ AUTHORS', '§ INGESTION', '§ AUTOMATION',
        '§ PULSE', '§ TIMER', '§ PROVIDERS', '§ INTEGRATIONS',
        '§ EXTRACTION TEMPLATES', '§ APPEARANCE',
    ];
    for (const marker of forbidden) {
        await expect(main.locator(`text="${marker}"`)).toHaveCount(0);
    }
  });

  test('topics list loads', async ({ page }) => {
    // Either we see topics listed or an empty state
    const topicCards = page.locator('[class*="card"]').filter({ hasText: /Enabled|Disabled/ });
    const emptyState = page.getByText('No topics');

    // Wait for loading to complete
    await expect(page.getByText('Loading topics...')).not.toBeVisible({ timeout: 10000 });

    // One of these should be visible
    const hasTopics = await topicCards.first().isVisible().catch(() => false);
    const hasEmptyState = await emptyState.isVisible().catch(() => false);
    expect(hasTopics || hasEmptyState).toBeTruthy();
  });

  test('create new topic via form', async ({ page }) => {
    // Wait for loading to finish
    await expect(page.getByText('Loading topics...')).not.toBeVisible({ timeout: 10000 });

    // Click "Add Topic" button to open the form
    await page.getByRole('button', { name: 'Add Topic' }).click();

    // Fill in the topic form
    await page.getByLabel('Name').fill('Test Topic E2E');
    await page.getByLabel('Query Terms').fill('testing, playwright, e2e');
    await page.getByLabel('Category').fill('Testing');

    // Submit the form — the submit button also says "Add Topic"
    await page.getByRole('button', { name: 'Add Topic' }).first().click();

    // Verify the new topic appears in the list
    await expect(page.getByText('Test Topic E2E')).toBeVisible({ timeout: 10000 });
  });

  test('edit existing topic', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading topics...')).not.toBeVisible({ timeout: 10000 });

    // If no topics exist, create one first
    const hasTopics = await page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .first()
      .isVisible()
      .catch(() => false);

    if (!hasTopics) {
      await page.getByRole('button', { name: 'Add Topic' }).click();
      await page.getByLabel('Name').fill('Editable Topic');
      await page.getByLabel('Query Terms').fill('edit, test');
      await page.getByRole('button', { name: 'Add Topic' }).first().click();
      await expect(page.getByText('Editable Topic')).toBeVisible({ timeout: 10000 });
    }

    // Click the pencil/edit icon on the first topic card
    const firstTopicCard = page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .first();
    await firstTopicCard.getByRole('button').filter({ has: page.locator('svg') }).nth(1).click();

    // The edit mode shows input fields inline
    const nameInput = firstTopicCard.getByPlaceholder('Name');
    await expect(nameInput).toBeVisible();

    // Modify the name
    await nameInput.clear();
    await nameInput.fill('Updated Topic Name');

    // Click save (check icon button)
    await firstTopicCard.getByRole('button').first().click();

    // Verify updated name appears
    await expect(page.getByText('Updated Topic Name')).toBeVisible({ timeout: 10000 });
  });

  test('delete topic', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading topics...')).not.toBeVisible({ timeout: 10000 });

    // If no topics exist, create one first
    const hasTopics = await page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .first()
      .isVisible()
      .catch(() => false);

    if (!hasTopics) {
      await page.getByRole('button', { name: 'Add Topic' }).click();
      await page.getByLabel('Name').fill('Delete Me Topic');
      await page.getByLabel('Query Terms').fill('delete, test');
      await page.getByRole('button', { name: 'Add Topic' }).first().click();
      await expect(page.getByText('Delete Me Topic')).toBeVisible({ timeout: 10000 });
    }

    // Count topics before delete
    const topicCountBefore = await page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .count();

    // Click the trash/delete icon on the first topic
    const firstTopicCard = page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .first();
    // The delete button is the last icon button in the row
    await firstTopicCard.getByRole('button').filter({ has: page.locator('svg') }).last().click();

    // Confirm deletion dialog should appear
    await expect(page.getByText('Delete Topic')).toBeVisible();
    await page.getByRole('button', { name: 'Delete' }).click();

    // Wait for the topic count to decrease or empty state to appear
    await expect(async () => {
      const currentCount = await page.locator('[class*="card"]')
        .filter({ hasText: /Enabled|Disabled/ })
        .count();
      const emptyVisible = await page.getByText('No topics').isVisible().catch(() => false);
      expect(currentCount < topicCountBefore || emptyVisible).toBeTruthy();
    }).toPass({ timeout: 10000 });
  });

  test('toggle topic enabled/disabled', async ({ page }) => {
    // Wait for loading
    await expect(page.getByText('Loading topics...')).not.toBeVisible({ timeout: 10000 });

    // If no topics exist, create one first
    const hasTopics = await page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .first()
      .isVisible()
      .catch(() => false);

    if (!hasTopics) {
      await page.getByRole('button', { name: 'Add Topic' }).click();
      await page.getByLabel('Name').fill('Toggle Topic');
      await page.getByLabel('Query Terms').fill('toggle, test');
      await page.getByRole('button', { name: 'Add Topic' }).first().click();
      await expect(page.getByText('Toggle Topic')).toBeVisible({ timeout: 10000 });
    }

    const firstTopicCard = page.locator('[class*="card"]')
      .filter({ hasText: /Enabled|Disabled/ })
      .first();

    // Check current state and toggle
    const isEnabled = await firstTopicCard.getByText('Enabled').isVisible().catch(() => false);

    if (isEnabled) {
      await firstTopicCard.getByRole('button', { name: 'Disable' }).click();
      await expect(firstTopicCard.getByText('Disabled')).toBeVisible({ timeout: 10000 });
    } else {
      await firstTopicCard.getByRole('button', { name: 'Enable' }).click();
      await expect(firstTopicCard.getByText('Enabled')).toBeVisible({ timeout: 10000 });
    }
  });
});
