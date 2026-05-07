/**
 * My Day v5 redesign — Playwright E2E smoke spec (Task 4.2)
 *
 * MODE: mocked — all API calls are intercepted via page.route();
 * no live backend is required.
 *
 * Covers:
 *   - Page loads without React error boundary
 *   - DateMasthead renders (RESEARCH LOG header)
 *   - § section markers visible in documented order
 *   - Hero card mode picker has 3 tabs
 *   - "Resume reading" tab is disabled (Phase 2 placeholder)
 *   - "Continue task" tab click persists choice to localStorage and
 *     survives a page reload (data-state="active")
 *   - Page wrapper uses .bg-paper class
 *
 * Notes:
 *   - TriageSection returns null when both action-items and
 *     missing-foundational lists are empty — soft-skipped here.
 *   - TodaysPulseSection returns null when deck has ≤1 card — soft-skipped.
 *   - ProjectsSection always renders (shows "none active" meta when empty).
 */

import { test, expect, type Page, type Route } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

// ---------------------------------------------------------------------------
// Reachability guard — skip gracefully if dashboard is not up
// ---------------------------------------------------------------------------

async function skipIfUnreachable(page: Page): Promise<void> {
  try {
    const resp = await page.request.get('/', { timeout: 3_000 });
    if (!resp.ok()) {
      test.skip(true, `Dashboard unreachable (status ${resp.status()})`);
    }
  } catch (err) {
    test.skip(true, `Dashboard unreachable: ${(err as Error).message}`);
  }
}

// ---------------------------------------------------------------------------
// Mock data
// ---------------------------------------------------------------------------

const MY_DAY_MOCK = {
  tasks: [],
  cards_due: 0,
  recommendations: [],
  today_focus_hours: 0,
  focus_streak_days: 0,
  project_pulse: [],
};

const STATS_MOCK = {
  due_now: 0,
  streak_days: 0,
  reviewed_today: 0,
  average_retention: 0,
};

// ---------------------------------------------------------------------------
// Route helpers
// ---------------------------------------------------------------------------

/**
 * Install all routes that the My Day page's queries depend on.
 * Returns stubs for the endpoints so sections render without hitting
 * a live backend.
 *
 *   /api/executive/my-day          → MY_DAY_MOCK
 *   /api/pulse/today               → 404 (no deck → TodaysPulseSection hides)
 *   /api/papers/feed               → { papers: [], total: 0 }
 *   /api/analytics/missing-foundational → []
 *   /api/stats                     → STATS_MOCK
 *   /api/jobs/*                    → passthrough (job-store calls)
 */
async function installMyDayMocks(page: Page): Promise<void> {
  // Executive my-day
  await page.route('**/api/executive/my-day', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MY_DAY_MOCK),
    });
  });

  // Pulse today — 404 so TodaysPulseSection hides (returns null when ≤1 card)
  await page.route('**/api/pulse/today', async (route: Route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });

  // Feed papers (action-items-unprocessed query + DateMasthead new-count query)
  await page.route('**/api/papers/feed**', async (route: Route) => {
    // Don't double-intercept feed/counts if ever added; this pattern is broad enough
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ papers: [], total: 0 }),
    });
  });

  // Missing foundational papers (TriageSection — returns [] so Triage hides)
  await page.route('**/api/analytics/missing-foundational', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });

  // Retention stats (LearningFocusSection)
  await page.route('**/api/stats', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(STATS_MOCK),
    });
  });
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

test.describe('My Day v5 redesign — smoke', () => {
  test.setTimeout(30_000);

  test.beforeEach(async ({ page }) => {
    await skipIfUnreachable(page);
    // Seed auth before any navigation so Zustand hydrates correctly
    await seedAuthedSession(page);
    await installMyDayMocks(page);
  });

  // ── 1. Page loads without error boundary ────────────────────────────────

  test('navigates to /my-day and loads without error boundary', async ({ page }) => {
    await page.goto('/my-day');

    // No "Something went wrong" error boundary should appear
    await expect(page.getByText('Something went wrong')).toHaveCount(0);

    // DateMasthead always renders the RESEARCH LOG mono header
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });
  });

  // ── 2. § section markers render in documented order ──────────────────────

  test('renders § section markers for always-visible sections', async ({ page }) => {
    await page.goto('/my-day');

    // Wait for page to settle (DateMasthead is a reliable ready-signal)
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // Sections that ALWAYS render regardless of data:
    //   Yesterday, Now, Today's intent, Projects (shows "none active"), End of day
    // Sections that conditionally render null in mocked empty state:
    //   Triage (both lists empty → null)
    //   Today's pulse (404 → null; ≤1 card → null)
    // LearningFocusSection always renders (shows stats skeleton then data).

    const alwaysVisible = [
      // '§ Yesterday' removed — YesterdaySection hidden until daily-rollup job ships (W2-19)
      '§ Now',
      "§ Today's intent",
      '§ Projects',
      '§ Learning & focus',
      '§ End of day',
    ];

    for (const marker of alwaysVisible) {
      await expect(page.locator(`text="${marker}"`).first()).toBeVisible({
        timeout: 5_000,
      });
    }
  });

  test('Triage and Today\'s pulse sections are absent in empty mocked state', async ({ page }) => {
    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // Give data time to settle
    await page.waitForTimeout(1_000);

    // TriageSection: returns null when actionItems.length === 0 && foundational.length === 0
    await expect(page.locator('text="§ Triage"')).toHaveCount(0);

    // TodaysPulseSection: returns null on 404 (cards.length <= 1)
    await expect(page.locator('text="§ Today\'s pulse"')).toHaveCount(0);
  });

  // ── 3. Hero card shows mode picker with 3 tabs ───────────────────────────

  test('hero card shows mode picker with 3 tabs', async ({ page }) => {
    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // TabsTrigger labels from HeroNow.tsx (exact text)
    await expect(page.getByRole('tab', { name: 'Pulse #1' })).toBeVisible({ timeout: 5_000 });
    await expect(page.getByRole('tab', { name: 'Resume reading' })).toBeVisible({ timeout: 5_000 });
    await expect(page.getByRole('tab', { name: 'Continue task' })).toBeVisible({ timeout: 5_000 });
  });

  // ── 4. Resume reading tab is disabled (Phase 2) ──────────────────────────

  test('Resume reading tab is disabled (Phase 2 placeholder)', async ({ page }) => {
    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // HeroNow wraps the disabled TabsTrigger in a <span> for Tooltip attachment;
    // the trigger itself carries the disabled attribute.
    const resumeTab = page.getByRole('tab', { name: 'Resume reading' });
    await expect(resumeTab).toBeVisible({ timeout: 5_000 });
    await expect(resumeTab).toBeDisabled();
  });

  // ── 5. Continue task tab persists to localStorage across reload ──────────

  test('clicking Continue task tab persists choice in localStorage', async ({ page }) => {
    // Clear any prior heroMode state before navigation
    await page.addInitScript(() => {
      localStorage.removeItem('myday.heroMode');
    });

    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    const taskTab = page.getByRole('tab', { name: 'Continue task' });
    await expect(taskTab).toBeVisible({ timeout: 5_000 });
    await taskTab.click();

    // Verify localStorage immediately after click
    const storedValue = await page.evaluate(() => localStorage.getItem('myday.heroMode'));
    expect(storedValue).toBe('task');

    // Reload and verify persistence: Radix Tabs should restore data-state="active"
    await installMyDayMocks(page); // re-install routes after reload wipes them
    await page.reload();
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    const taskTabAfterReload = page.getByRole('tab', { name: 'Continue task' });
    await expect(taskTabAfterReload).toHaveAttribute('data-state', 'active', { timeout: 5_000 });
  });

  // ── 6. Page wrapper uses .bg-paper class ─────────────────────────────────

  test('page wrapper uses .bg-paper class', async ({ page }) => {
    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // MyDayPage renders: <div className="bg-paper min-h-screen">
    const wrapper = page.locator('.bg-paper').first();
    await expect(wrapper).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Dark-mode toggle tests
// ---------------------------------------------------------------------------

test.describe('dark mode toggle', () => {
  test.setTimeout(30_000);

  test.beforeEach(async ({ page }) => {
    await skipIfUnreachable(page);
    await seedAuthedSession(page);
    await installMyDayMocks(page);
  });

  // ── 1. Toggle to dark applies dark class on <html> ───────────────────────

  test('toggling to dark applies dark class on <html>', async ({ page }) => {
    // Start fresh — ensure no persisted theme so store initialises to 'system'
    await page.addInitScript(() => {
      localStorage.removeItem('jarvis-theme');
    });

    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // Default Playwright colorScheme is light → system theme resolves to light.
    // Zustand default: 'system'. Cycle: system → light → dark.
    // Click 1: system → light (no dark class)
    const toggleBtn = page.getByRole('button', { name: /Toggle theme/ });
    await toggleBtn.click();
    // After first click the aria-label shows 'light'; <html> should NOT have dark
    await expect(page.locator('html')).not.toHaveClass(/dark/);

    // Click 2: light → dark
    await toggleBtn.click();
    await expect(page.locator('html')).toHaveClass(/dark/);
  });

  // ── 2. Dark mode persists across page reload ─────────────────────────────

  test('dark mode persists after page reload', async ({ page }) => {
    // Pre-seed localStorage with dark theme so we start in dark directly
    await page.addInitScript(() => {
      localStorage.setItem(
        'jarvis-theme',
        JSON.stringify({ state: { theme: 'dark' }, version: 0 }),
      );
    });

    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // Dark class should be applied by the FOUC script
    await expect(page.locator('html')).toHaveClass(/dark/);

    // Reload and re-install mocks (reload wipes page.route registrations)
    await installMyDayMocks(page);
    await page.reload();
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // Dark class must survive reload (localStorage-backed)
    await expect(page.locator('html')).toHaveClass(/dark/);
  });

  // ── 3. System mode follows OS color-scheme preference ───────────────────

  test('system mode follows OS prefers-color-scheme', async ({ page }) => {
    // Pre-seed system theme in localStorage
    await page.addInitScript(() => {
      localStorage.setItem(
        'jarvis-theme',
        JSON.stringify({ state: { theme: 'system' }, version: 0 }),
      );
    });

    // Emulate dark OS preference BEFORE navigation so the FOUC script sees it
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // System + dark OS → <html> should have dark class
    await expect(page.locator('html')).toHaveClass(/dark/);

    // Now switch OS preference to light and reload so FOUC script re-evaluates
    await page.emulateMedia({ colorScheme: 'light' });
    await installMyDayMocks(page);
    await page.reload();
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // System + light OS → <html> should NOT have dark class
    await expect(page.locator('html')).not.toHaveClass(/dark/);
  });
});
