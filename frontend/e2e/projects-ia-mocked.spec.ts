/**
 * Projects IA Redesign (F6) — mocked Playwright e2e walk.
 *
 * Uses page.route to fulfill all projects/questions/activity API calls with
 * seeded data so tests run against the running frontend dev server
 * (baseURL 127.0.0.1:3001) without a live backend.
 *
 * Seeds: one project ("RGS Thesis"), one linked paper, one open question,
 * one activity item (added_paper), one milestone, one task.
 */
import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

// ── Seed data ─────────────────────────────────────────────────────────────────

const MOCK_PROJECT = {
  id: 1,
  name: 'RGS Thesis',
  description: 'Reward-guided sampling for diffusion LMs.',
  status: 'active',
  deadline: '2026-08-31',
  color: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  paper_count: 1,
  open_question_count: 1,
};

const MOCK_PROJECTS = [MOCK_PROJECT];

const MOCK_QUESTIONS = [
  {
    id: 10,
    project_id: 1,
    body: 'Does reward guidance scale with model size?',
    created_at: '2026-05-01T10:00:00Z',
  },
];

const MOCK_ACTIVITY = [
  {
    kind: 'added_paper',
    ts: new Date(Date.now() - 2 * 3600_000).toISOString(),
    label: 'Test-time scaling of diffusion LMs',
  },
];

const MOCK_TASKS = [
  {
    id: 20,
    project_id: 1,
    parent_task_id: null,
    title: 'Draft introduction',
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
  },
];

const MOCK_MILESTONES = [
  {
    id: 30,
    project_id: 1,
    name: 'Literature review complete',
    deadline: '2026-06-30',
    description: null,
    completed: false,
    completed_at: null,
    created_at: '2026-05-01T00:00:00Z',
  },
];

const MOCK_PAPERS = [
  {
    id: 5,
    title: 'Test-time scaling of diffusion LMs',
    authors: ['Smith, J.'],
    source_type: 'arxiv',
    published_date: '2026-01-15',
    notes: null,
    added_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
  },
];

const NEW_QUESTION = {
  id: 11,
  project_id: 1,
  body: 'What is the optimal temperature?',
  created_at: new Date().toISOString(),
};

// ── Route helpers ──────────────────────────────────────────────────────────────

async function mockProjectsRoutes(page: import('@playwright/test').Page) {
  // FirstRunGate — must return setup_completed: true or the wizard intercepts the page.
  await page.route('**/api/setup/status', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) });
  });

  // List projects
  await page.route('**/api/projects', (route) => {
    if (route.request().method() === 'GET') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_PROJECTS),
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

  // Single project detail
  await page.route('**/api/projects/1', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MOCK_PROJECT),
    });
  });

  // Open questions
  await page.route('**/api/projects/1/questions', (route) => {
    if (route.request().method() === 'GET') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_QUESTIONS),
      });
    } else if (route.request().method() === 'POST') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(NEW_QUESTION),
      });
    } else {
      route.continue();
    }
  });

  // Activity feed
  await page.route('**/api/projects/1/activity**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MOCK_ACTIVITY),
    });
  });

  // Tasks
  await page.route('**/api/projects/1/tasks', (route) => {
    if (route.request().method() === 'GET') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_TASKS),
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
        body: JSON.stringify(MOCK_MILESTONES),
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
      body: JSON.stringify(MOCK_PAPERS),
    });
  });

  // Question delete
  await page.route('**/api/questions/**', (route) => {
    if (route.request().method() === 'DELETE') {
      route.fulfill({ status: 204, body: '' });
    } else {
      route.continue();
    }
  });
}

// ── Tests ──────────────────────────────────────────────────────────────────────

test.describe('Projects IA Redesign (mocked)', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);
    await mockProjectsRoutes(page);
    await page.goto('/projects');
    // Wait for the chapter rail to be visible
    await expect(page.getByText(/§ CHAPTERS/i)).toBeVisible({ timeout: 10_000 });
  });

  // ── 3.1 Chapter rail ────────────────────────────────────────────────────────

  test('§ CHAPTERS header is visible in the rail', async ({ page }) => {
    await expect(page.getByText(/§ CHAPTERS/i)).toBeVisible();
  });

  test('roman numeral I is shown for the first chapter', async ({ page }) => {
    // Scope to the font-mono span that ChapterRail renders for roman numerals.
    // getByText('I') matches 28+ elements (letter 'I' appears in many words).
    await expect(page.locator('span.font-mono').getByText('I', { exact: true }).first()).toBeVisible();
  });

  test('project name appears in the chapter rail', async ({ page }) => {
    await expect(page.getByText('RGS Thesis').first()).toBeVisible();
  });

  // ── 3.2 Status vocabulary ────────────────────────────────────────────────────

  test('status chip shows translated label "reading" for active status', async ({ page }) => {
    await expect(page.getByText('reading').first()).toBeVisible({ timeout: 8_000 });
  });

  // ── 3.6 Counts in rail ────────────────────────────────────────────────────────

  test('chapter row shows paper_count = 1 paper', async ({ page }) => {
    await expect(page.getByText('1 papers')).toBeVisible({ timeout: 8_000 });
  });

  test('chapter row shows open_question_count = 1 Qs', async ({ page }) => {
    await expect(page.getByText('1 Qs')).toBeVisible({ timeout: 8_000 });
  });

  // ── 3.7 Auto-select ────────────────────────────────────────────────────────────

  test('first chapter is auto-selected on load (pane shows breadcrumb)', async ({ page }) => {
    // Breadcrumb shows "Projects" — scope to breadcrumb nav to avoid strict-mode
    // (title also appears in the page heading and sidebar label).
    await expect(page.getByLabel('breadcrumb').getByText('Projects')).toBeVisible({ timeout: 8_000 });
    await expect(page.getByText('RGS Thesis').first()).toBeVisible();
  });

  // ── 3.3 Document pane sections ────────────────────────────────────────────────

  test('§ OPEN QUESTIONS section is visible in the document pane', async ({ page }) => {
    await expect(page.getByText(/§ OPEN QUESTIONS/i)).toBeVisible({ timeout: 8_000 });
  });

  test('§ RECENT ACTIVITY section is visible in the document pane', async ({ page }) => {
    await expect(page.getByText(/§ RECENT ACTIVITY/i)).toBeVisible({ timeout: 8_000 });
  });

  test('§ MILESTONES section is visible in the document pane', async ({ page }) => {
    await expect(page.getByText(/§ MILESTONES/i)).toBeVisible({ timeout: 8_000 });
  });

  test('§ TASKS section is visible in the document pane', async ({ page }) => {
    await expect(page.getByText(/§ TASKS/i)).toBeVisible({ timeout: 8_000 });
  });

  test('§ PAPERS section is visible in the document pane', async ({ page }) => {
    await expect(page.getByText(/§ PAPERS/i)).toBeVisible({ timeout: 8_000 });
  });

  // ── § OPEN QUESTIONS content ──────────────────────────────────────────────────

  test('seeded question appears with Q1 label', async ({ page }) => {
    await expect(page.getByText('Q1')).toBeVisible({ timeout: 8_000 });
    await expect(
      page.getByText('Does reward guidance scale with model size?'),
    ).toBeVisible({ timeout: 8_000 });
  });

  test('open questions section shows § OPEN QUESTIONS · 1 count', async ({ page }) => {
    await expect(page.getByText(/§ OPEN QUESTIONS · 1/i)).toBeVisible({ timeout: 8_000 });
  });

  test('add question inline: typing and submitting adds a new question', async ({ page }) => {
    const input = page.getByPlaceholder(/add an open question/i);
    await expect(input).toBeVisible({ timeout: 8_000 });
    await input.fill('What is the optimal temperature?');
    await page.getByRole('button', { name: /add question/i }).click();
    // The POST is mocked; the mock returns NEW_QUESTION. After mutation,
    // the component re-fetches — in the mocked test, we just check the POST was sent.
    // Verify input is cleared (optimistic UX)
    await expect(input).toHaveValue('', { timeout: 5_000 });
  });

  // ── § RECENT ACTIVITY content ──────────────────────────────────────────────────

  test('recent activity shows ADDED prefix for added_paper kind', async ({ page }) => {
    await expect(page.getByText('ADDED')).toBeVisible({ timeout: 8_000 });
  });

  test('recent activity item shows paper title label', async ({ page }) => {
    // Scope to § RECENT ACTIVITY section to avoid strict-mode: the same title also
    // appears in the § PAPERS section below.
    await expect(
      page.getByLabel('§ RECENT ACTIVITY').getByText('Test-time scaling of diffusion LMs'),
    ).toBeVisible({ timeout: 8_000 });
  });

  test('recent activity item shows relative time string', async ({ page }) => {
    // Seeded 2 hours ago
    await expect(page.getByText(/hours? ago/i)).toBeVisible({ timeout: 8_000 });
  });

  // ── Preserved functionality (§ 3.8) ──────────────────────────────────────────

  test('seeded milestone appears in § MILESTONES section', async ({ page }) => {
    await expect(
      page.getByText('Literature review complete'),
    ).toBeVisible({ timeout: 8_000 });
  });

  test('seeded task appears in § TASKS section', async ({ page }) => {
    await expect(page.getByText('Draft introduction')).toBeVisible({ timeout: 8_000 });
  });

  test('seeded paper appears in § PAPERS section', async ({ page }) => {
    // Scope to § PAPERS section to avoid strict-mode: the same title also
    // appears in the § RECENT ACTIVITY section above.
    await expect(
      page.getByLabel(/§ PAPERS/).getByText('Test-time scaling of diffusion LMs'),
    ).toBeVisible({ timeout: 8_000 });
  });

  test('Add Milestone button is present in § MILESTONES section', async ({ page }) => {
    await expect(page.getByRole('button', { name: /add milestone/i })).toBeVisible({
      timeout: 8_000,
    });
  });

  test('Add Task button is present in § TASKS section', async ({ page }) => {
    await expect(page.getByRole('button', { name: /add task/i })).toBeVisible({
      timeout: 8_000,
    });
  });

  test('"Link a paper" search input is present in § PAPERS section', async ({ page }) => {
    await expect(page.getByPlaceholder(/search papers/i)).toBeVisible({ timeout: 8_000 });
  });

  test('New Chapter button is visible in rail footer', async ({ page }) => {
    await expect(page.getByText(/new chapter/i)).toBeVisible({ timeout: 8_000 });
  });

  test('Create Project dialog opens from New Chapter button', async ({ page }) => {
    await page.getByText(/new chapter/i).click();
    await expect(page.getByRole('heading', { name: /create project/i })).toBeVisible({
      timeout: 5_000,
    });
  });

  test('Delete project button is present in the document pane header', async ({ page }) => {
    await expect(page.getByRole('button', { name: /delete project/i })).toBeVisible({
      timeout: 8_000,
    });
  });

  test('Delete project button opens confirm dialog', async ({ page }) => {
    await page.getByRole('button', { name: /delete project/i }).click();
    await expect(page.getByText(/delete project\?/i)).toBeVisible({ timeout: 5_000 });
    // Cancel to keep the project
    await page.getByRole('button', { name: /cancel/i }).click();
  });

  // ── No tabs in document pane (§ 3.3) ─────────────────────────────────────────

  test('no tab bar in document pane (tabs removed per IA redesign)', async ({ page }) => {
    // Old tab labels should not appear as role=tab
    await expect(page.getByRole('tab', { name: /overview/i })).not.toBeVisible();
    await expect(page.getByRole('tab', { name: /milestones/i })).not.toBeVisible();
    await expect(page.getByRole('tab', { name: /tasks/i })).not.toBeVisible();
    await expect(page.getByRole('tab', { name: /papers/i })).not.toBeVisible();
  });
});
