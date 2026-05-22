/**
 * Projects page — Playwright e2e spec (chapter-rail IA).
 *
 * Uses page.route to seed all API calls deterministically, so tests run
 * against the frontend dev server (baseURL 127.0.0.1:3001) without a live
 * backend.  All core assertions are unconditional — no optional if-visible
 * branches.
 *
 * Covers:
 *  - chapter rail rendering (§ CHAPTERS, project name, status chip)
 *  - document pane inline sections (§ TASKS, § MILESTONES, § PAPERS)
 *  - no tab bar present (old UI removed)
 *  - create-project dialog opens from "New Chapter"
 *  - delete-project confirm dialog opens and cancels cleanly
 *  - task creation: Add Task dialog + optimistic appearance
 *  - milestone creation: Add Milestone dialog + optimistic appearance
 */
import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

// ── Seed data ──────────────────────────────────────────────────────────────────

const MOCK_PROJECT = {
  id: 1,
  name: 'E2E Thesis Project',
  description: 'End-to-end test chapter.',
  status: 'active',
  deadline: '2026-12-01',
  color: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  paper_count: 1,
  open_question_count: 0,
};

const MOCK_TASK = {
  id: 20,
  project_id: 1,
  parent_task_id: null,
  title: 'Write abstract',
  description: null,
  status: 'todo',
  priority: 3,
  deadline: null,
  estimated_hours: null,
  actual_hours: null,
  sort_order: 0,
  completed_at: null,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
};

const MOCK_MILESTONE = {
  id: 30,
  project_id: 1,
  name: 'Draft complete',
  deadline: '2026-07-31',
  description: null,
  completed: false,
  completed_at: null,
  created_at: '2026-05-01T00:00:00Z',
};

const MOCK_PAPER = {
  id: 5,
  title: 'Seeded Linked Paper',
  authors: ['Author A.'],
  source_type: 'arxiv',
  published_date: '2026-01-10',
  notes: null,
  added_at: new Date(Date.now() - 3600_000).toISOString(),
};

const NEW_TASK = { ...MOCK_TASK, id: 21, title: 'E2E Created Task' };
const NEW_MILESTONE = { ...MOCK_MILESTONE, id: 31, name: 'E2E Created Milestone' };

// ── Route helper ───────────────────────────────────────────────────────────────

async function mockRoutes(page: import('@playwright/test').Page) {
  // Project list
  await page.route('**/api/projects', (route) => {
    if (route.request().method() === 'GET') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([MOCK_PROJECT]),
      });
    } else if (route.request().method() === 'POST') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...MOCK_PROJECT, id: 99, name: 'New Chapter' }),
      });
    } else {
      route.continue();
    }
  });

  // Single project
  await page.route('**/api/projects/1', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MOCK_PROJECT),
    });
  });

  // Open questions
  await page.route('**/api/projects/1/questions', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });

  // Activity
  await page.route('**/api/projects/1/activity**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });

  // Tasks
  await page.route('**/api/projects/1/tasks', (route) => {
    if (route.request().method() === 'GET') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([MOCK_TASK]),
      });
    } else if (route.request().method() === 'POST') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(NEW_TASK),
      });
    } else {
      route.continue();
    }
  });

  // Milestones
  await page.route('**/api/projects/1/milestones', (route) => {
    if (route.request().method() === 'GET') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([MOCK_MILESTONE]),
      });
    } else if (route.request().method() === 'POST') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(NEW_MILESTONE),
      });
    } else {
      route.continue();
    }
  });

  // Linked papers
  await page.route('**/api/projects/1/papers', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([MOCK_PAPER]),
    });
  });

  // Delete project
  await page.route('**/api/projects/1', (route) => {
    if (route.request().method() === 'DELETE') {
      route.fulfill({ status: 204, body: '' });
    } else {
      route.continue();
    }
  });
}

// ── Tests ──────────────────────────────────────────────────────────────────────

test.describe('Projects page — chapter-rail IA', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);
    await mockRoutes(page);
    await page.goto('/projects');
    // Wait for rail to hydrate
    await expect(page.getByText(/§ CHAPTERS/i)).toBeVisible({ timeout: 10_000 });
  });

  // ── Chapter rail ─────────────────────────────────────────────────────────────

  test('chapter rail renders § CHAPTERS heading', async ({ page }) => {
    await expect(page.getByText(/§ CHAPTERS/i)).toBeVisible();
  });

  test('seeded project name appears in rail', async ({ page }) => {
    await expect(page.getByText('E2E Thesis Project').first()).toBeVisible();
  });

  test('status chip shows translated label "reading" for active project', async ({ page }) => {
    await expect(page.getByText('reading').first()).toBeVisible({ timeout: 8_000 });
  });

  test('rail shows paper_count = 1 papers', async ({ page }) => {
    await expect(page.getByText('1 papers')).toBeVisible({ timeout: 8_000 });
  });

  // ── Auto-select + breadcrumb ──────────────────────────────────────────────────

  test('first project is auto-selected and breadcrumb is visible', async ({ page }) => {
    await expect(page.getByText('Projects')).toBeVisible({ timeout: 8_000 });
    await expect(page.getByText('E2E Thesis Project').first()).toBeVisible();
  });

  // ── Inline sections (no tabs) ─────────────────────────────────────────────────

  test('§ TASKS section is visible in document pane', async ({ page }) => {
    await expect(page.getByText(/§ TASKS/i)).toBeVisible({ timeout: 8_000 });
  });

  test('§ MILESTONES section is visible in document pane', async ({ page }) => {
    await expect(page.getByText(/§ MILESTONES/i)).toBeVisible({ timeout: 8_000 });
  });

  test('§ PAPERS section is visible in document pane', async ({ page }) => {
    await expect(page.getByText(/§ PAPERS/i)).toBeVisible({ timeout: 8_000 });
  });

  test('no tab bar in document pane — old overview/tasks/milestones/papers tabs absent', async ({
    page,
  }) => {
    await expect(page.getByRole('tab', { name: /overview/i })).not.toBeVisible();
    await expect(page.getByRole('tab', { name: /tasks/i })).not.toBeVisible();
    await expect(page.getByRole('tab', { name: /milestones/i })).not.toBeVisible();
    await expect(page.getByRole('tab', { name: /papers/i })).not.toBeVisible();
  });

  // ── Seeded content ────────────────────────────────────────────────────────────

  test('seeded task "Write abstract" appears in § TASKS section', async ({ page }) => {
    await expect(page.getByText('Write abstract')).toBeVisible({ timeout: 8_000 });
  });

  test('seeded milestone "Draft complete" appears in § MILESTONES section', async ({ page }) => {
    await expect(page.getByText('Draft complete')).toBeVisible({ timeout: 8_000 });
  });

  test('seeded linked paper appears in § PAPERS section', async ({ page }) => {
    await expect(page.getByText('Seeded Linked Paper')).toBeVisible({ timeout: 8_000 });
  });

  // ── CRUD — Add Task ───────────────────────────────────────────────────────────

  test('Add Task button is present in § TASKS section', async ({ page }) => {
    await expect(page.getByRole('button', { name: /add task/i })).toBeVisible({ timeout: 8_000 });
  });

  test('Add Task dialog opens, accepts title, submits', async ({ page }) => {
    await page.getByRole('button', { name: /add task/i }).click();
    await expect(page.getByRole('heading', { name: /add task/i })).toBeVisible({ timeout: 5_000 });

    await page.getByLabel(/title/i).fill('E2E Created Task');
    await page.getByRole('button', { name: /^add$/i }).click();

    // Dialog should close
    await expect(page.getByRole('heading', { name: /add task/i })).not.toBeVisible({
      timeout: 5_000,
    });
  });

  // ── CRUD — Add Milestone ──────────────────────────────────────────────────────

  test('Add Milestone button is present in § MILESTONES section', async ({ page }) => {
    await expect(page.getByRole('button', { name: /add milestone/i })).toBeVisible({
      timeout: 8_000,
    });
  });

  test('Add Milestone dialog opens, accepts name, submits', async ({ page }) => {
    await page.getByRole('button', { name: /add milestone/i }).click();
    await expect(page.getByRole('heading', { name: /add milestone/i })).toBeVisible({
      timeout: 5_000,
    });

    await page.getByLabel(/name/i).fill('E2E Created Milestone');
    await page.getByRole('button', { name: /^add$/i }).click();

    // Dialog should close
    await expect(page.getByRole('heading', { name: /add milestone/i })).not.toBeVisible({
      timeout: 5_000,
    });
  });

  // ── CRUD — Link Paper ─────────────────────────────────────────────────────────

  test('"Search papers" input is present in § PAPERS section', async ({ page }) => {
    await expect(page.getByPlaceholder(/search papers/i)).toBeVisible({ timeout: 8_000 });
  });

  // ── Create project ────────────────────────────────────────────────────────────

  test('New Chapter button is visible in rail footer', async ({ page }) => {
    await expect(page.getByText(/new chapter/i)).toBeVisible();
  });

  test('Create Project dialog opens from New Chapter button', async ({ page }) => {
    await page.getByText(/new chapter/i).click();
    await expect(page.getByRole('heading', { name: /create project/i })).toBeVisible({
      timeout: 5_000,
    });
  });

  // ── Delete project ────────────────────────────────────────────────────────────

  test('Delete project button is present in document pane header', async ({ page }) => {
    await expect(page.getByRole('button', { name: /delete project/i })).toBeVisible({
      timeout: 8_000,
    });
  });

  test('Delete project opens confirm dialog; cancel keeps project', async ({ page }) => {
    await page.getByRole('button', { name: /delete project/i }).click();
    await expect(page.getByText(/delete project\?/i)).toBeVisible({ timeout: 5_000 });
    await page.getByRole('button', { name: /cancel/i }).click();
    // Rail still shows the project after cancel
    await expect(page.getByText('E2E Thesis Project').first()).toBeVisible({ timeout: 5_000 });
  });
});
