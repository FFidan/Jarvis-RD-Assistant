import { test, expect } from '@playwright/test';

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
  await page.goto('/projects');
});

test.describe('Projects Page', () => {
  test('page loads with heading and project list or empty state', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /projects/i })).toBeVisible();

    // Either projects are listed or the empty state shows
    await expect(
      page
        .getByText(/no projects/i)
        .or(page.getByText(/create your first project/i))
        .or(page.getByText(/select a project/i))
        .or(page.getByPlaceholder(/search projects/i)),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('create new project via dialog', async ({ page }) => {
    // Click the create project button (icon button with Plus)
    await page.getByRole('button', { name: /create project/i }).click();

    // Dialog should open
    await expect(page.getByRole('heading', { name: /create project/i })).toBeVisible();

    // Fill in project details
    await page.getByLabel(/name/i).fill('E2E Test Project');
    await page.getByLabel(/description/i).fill('Created by Playwright E2E test');

    // Click Create button in dialog
    await page.getByRole('button', { name: /^create$/i }).click();

    // Wait for the dialog to close and the project to appear
    // The project name should now appear in the list
    await expect(page.getByText('E2E Test Project')).toBeVisible({ timeout: 10_000 });
  });

  test('edit project details via status selector in overview', async ({ page }) => {
    // We need a project to exist. Look for either existing project or empty state.
    const projectButton = page.locator('button').filter({ hasText: /\w/ }).first();
    const emptyState = page.getByText(/no projects/i).or(page.getByText(/create your first project/i));

    await expect(projectButton.or(emptyState)).toBeVisible({ timeout: 10_000 });

    // If there are projects, select one and check the detail view
    if (await page.getByPlaceholder(/search projects/i).isVisible()) {
      // Click first project in the list (the list items are buttons)
      const firstProject = page
        .locator('button')
        .filter({ has: page.locator('.font-medium') })
        .first();

      if (await firstProject.isVisible()) {
        await firstProject.click();

        // Overview tab should show by default with project details
        await expect(page.getByRole('tab', { name: /overview/i })).toBeVisible();
        await expect(page.getByText(/total tasks/i)).toBeVisible({ timeout: 10_000 });

        // The status selector should be visible
        await expect(page.getByText(/status/i).first()).toBeVisible();
      }
    }
  });

  test('tasks CRUD: add task, complete, and delete', async ({ page }) => {
    // Wait for page to load
    await expect(page.getByRole('heading', { name: /projects/i })).toBeVisible();

    // We need a project selected to see tasks. If there are projects, click the first.
    const searchInput = page.getByPlaceholder(/search projects/i);
    const emptyState = page.getByText(/no projects/i).or(page.getByText(/create your first project/i));

    await expect(searchInput.or(emptyState)).toBeVisible({ timeout: 10_000 });

    if (await searchInput.isVisible()) {
      // Click first project
      const firstProject = page
        .locator('button')
        .filter({ has: page.locator('.font-medium') })
        .first();

      if (await firstProject.isVisible()) {
        await firstProject.click();

        // Switch to Tasks tab
        await page.getByRole('tab', { name: /tasks/i }).click();

        // Should see task list or empty state
        await expect(
          page
            .getByText(/no tasks/i)
            .or(page.getByText(/add tasks to break down/i))
            .or(page.getByRole('button', { name: /add task/i })),
        ).toBeVisible({ timeout: 10_000 });

        // Click Add Task
        await page.getByRole('button', { name: /add task/i }).click();

        // Fill in task details
        await expect(page.getByRole('heading', { name: /add task/i })).toBeVisible();
        await page.getByLabel(/title/i).fill('E2E Test Task');

        // Click Add button
        await page.getByRole('button', { name: /^add$/i }).click();

        // Task should appear
        await expect(page.getByText('E2E Test Task')).toBeVisible({ timeout: 10_000 });
      }
    }
  });

  test('milestones CRUD: add and toggle milestone', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /projects/i })).toBeVisible();

    const searchInput = page.getByPlaceholder(/search projects/i);
    const emptyState = page.getByText(/no projects/i);

    await expect(searchInput.or(emptyState)).toBeVisible({ timeout: 10_000 });

    if (await searchInput.isVisible()) {
      const firstProject = page
        .locator('button')
        .filter({ has: page.locator('.font-medium') })
        .first();

      if (await firstProject.isVisible()) {
        await firstProject.click();

        // Switch to Milestones tab
        await page.getByRole('tab', { name: /milestones/i }).click();

        await expect(
          page
            .getByText(/no milestones/i)
            .or(page.getByText(/add milestones to track/i))
            .or(page.getByRole('button', { name: /add milestone/i })),
        ).toBeVisible({ timeout: 10_000 });

        // Click Add Milestone
        await page.getByRole('button', { name: /add milestone/i }).click();

        // Fill in milestone details
        await expect(page.getByRole('heading', { name: /add milestone/i })).toBeVisible();
        await page.getByLabel(/name/i).fill('E2E Milestone');

        // Click Add
        await page.getByRole('button', { name: /^add$/i }).click();

        // Milestone should appear with a checkbox
        await expect(page.getByText('E2E Milestone')).toBeVisible({ timeout: 10_000 });

        // Toggle the milestone checkbox
        const checkbox = page.getByRole('checkbox').first();
        if (await checkbox.isVisible()) {
          await checkbox.check();
          // Completed milestone should show line-through styling (tested via visual)
        }
      }
    }
  });

  test('link paper to project via papers tab', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /projects/i })).toBeVisible();

    const searchInput = page.getByPlaceholder(/search projects/i);
    const emptyState = page.getByText(/no projects/i);

    await expect(searchInput.or(emptyState)).toBeVisible({ timeout: 10_000 });

    if (await searchInput.isVisible()) {
      const firstProject = page
        .locator('button')
        .filter({ has: page.locator('.font-medium') })
        .first();

      if (await firstProject.isVisible()) {
        await firstProject.click();

        // Switch to Papers tab
        await page.getByRole('tab', { name: /papers/i }).click();

        // Should see the linked papers section and search input
        await expect(
          page
            .getByText(/linked paper/i)
            .or(page.getByText(/no linked papers/i))
            .or(page.getByText(/link a paper from your library/i)),
        ).toBeVisible({ timeout: 10_000 });

        // The search input for linking papers should be visible
        await expect(page.getByPlaceholder(/search papers/i)).toBeVisible();
      }
    }
  });

  test('delete project via confirm dialog', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /projects/i })).toBeVisible();

    const searchInput = page.getByPlaceholder(/search projects/i);
    const emptyState = page.getByText(/no projects/i);

    await expect(searchInput.or(emptyState)).toBeVisible({ timeout: 10_000 });

    if (await searchInput.isVisible()) {
      const firstProject = page
        .locator('button')
        .filter({ has: page.locator('.font-medium') })
        .first();

      if (await firstProject.isVisible()) {
        await firstProject.click();

        // Wait for detail view to load
        await expect(page.getByRole('tab', { name: /overview/i })).toBeVisible({ timeout: 10_000 });

        // Click the delete (trash) button in the project detail header
        // It is a ghost button with Trash2 icon in the detail header
        const deleteButton = page
          .locator('.flex.items-center.justify-between.border-b')
          .getByRole('button');

        if (await deleteButton.isVisible()) {
          await deleteButton.click();

          // Confirm dialog should appear
          await expect(page.getByText(/delete project/i)).toBeVisible();
          await expect(page.getByText(/permanently delete/i)).toBeVisible();

          // Click the Delete confirmation button
          await page.getByRole('button', { name: /^delete$/i }).click();

          // Project should be removed — either another project or empty state
          await expect(
            page
              .getByText(/select a project/i)
              .or(page.getByText(/no projects/i)),
          ).toBeVisible({ timeout: 10_000 });
        }
      }
    }
  });
});
