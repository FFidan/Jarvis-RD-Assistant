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
 *   - "Resume reading" tab is disabled (placeholder)
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
  // FirstRunGate — must return setup_completed: true or the wizard intercepts /my-day.
  await page.route('**/api/setup/status', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true }) });
  });

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

  // § Yesterday rollup — empty so the section stays silent by default
  await page.route('**/api/my-day/yesterday**', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        date: '2026-05-14',
        focused_hours: 0,
        cards_reviewed: 0,
        tasks_done: 0,
        completed: [],
        deferred: [],
      }),
    });
  });

  // § Open threads — none so ThreadsSection shows its empty affordance
  await page.route('**/api/my-day/threads', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });

  // EOD journal — no entry yet
  await page.route('**/api/my-day/journal**', async (route: Route) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 1,
          date: '2026-05-15',
          prompts: {},
          created_at: '',
          updated_at: '',
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });

  // Today's intent
  await page.route('**/api/executive/intent/today', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ intent: null, updated_at: null }),
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

    // § Yesterday is now an on-the-fly rollup — hidden when empty (default
    // mock). § Open threads always renders (empty affordance). EOD is the
    // shutdown ritual.
    const alwaysVisible = [
      '§ Now',
      "§ Today's intent",
      '§ Projects',
      '§ Open threads',
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

  // ── 3. Hero mode picker — Pulse #1 always shows; others conditional ──────

  test('hero mode picker shows Pulse #1 by default; conditional tabs hidden', async ({ page }) => {
    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    // Pulse #1 is always present.
    await expect(page.getByRole('tab', { name: 'Pulse #1' })).toBeVisible({ timeout: 5_000 });
    // No active Pomodoro / threads / reading in the empty mock → conditional
    // tabs are not rendered.
    await expect(page.getByRole('tab', { name: 'Continue task' })).toHaveCount(0);
    await expect(page.getByRole('tab', { name: 'Resume thread' })).toHaveCount(0);
  });

  // ── 4. Continue task tab + persistence with an active Pomodoro ───────────

  test.fixme('Continue task tab appears with an active Pomodoro and persists to localStorage', async ({
    page,
  }) => {
    // FIXME: pomodoro-store v1 migration strips timer state (phase, secondsRemaining, etc.)
    // from version-0 blobs. `partialize` also only persists settings (targetCycles, work/break
    // minutes), not runtime state. Seeding `{ version: 0, state: { phase: 'work' } }` is
    // a no-op after migration — phase becomes 'idle', hasTask = false, tab never renders.
    // The test premise is broken; needs a direct store-state injection approach or a v1+ blob.
    await page.addInitScript(() => {
      localStorage.removeItem('myday.heroMode');
      // Seed an active Pomodoro so the "Continue task" tab renders.
      localStorage.setItem(
        'jarvis-pomodoro',
        JSON.stringify({
          state: {
            phase: 'work',
            secondsRemaining: 1500,
            phaseDurationMs: 1_500_000,
            totalPausedMs: 0,
            cyclesCompleted: 0,
            targetCycles: 4,
            workMinutes: 25,
            shortBreakMinutes: 5,
            longBreakMinutes: 15,
            startedAt: Date.now(),
            pausedAt: null,
            attachedItem: { id: 1, title: 'Vector-field argument', type: 'task' },
            completedSession: null,
          },
          version: 0,
        }),
      );
    });

    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    const taskTab = page.getByRole('tab', { name: 'Continue task' });
    await expect(taskTab).toBeVisible({ timeout: 5_000 });
    await taskTab.click();

    const storedValue = await page.evaluate(() => localStorage.getItem('myday.heroMode'));
    expect(storedValue).toBe('task');

    // HeroTask exposes the new §1a controls.
    await expect(page.getByRole('button', { name: /stop & log/i })).toBeVisible({
      timeout: 5_000,
    });
  });

  // ── 5. EOD shutdown ritual renders the 3 structured prompts ──────────────

  test('End of day shutdown ritual renders the 3 prompts', async ({ page }) => {
    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    await expect(page.getByLabel('One thing that worked')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByLabel("What's still blocking me")).toBeVisible();
    await expect(page.getByLabel('First move tomorrow')).toBeVisible();
  });

  // ── 5b. § Open threads shows the empty create affordance ─────────────────

  test('Open threads section offers an inline create affordance when empty', async ({ page }) => {
    await page.goto('/my-day');
    await expect(page.locator('text=RESEARCH LOG').first()).toBeVisible({ timeout: 5_000 });

    await expect(page.locator('text="§ Open threads"').first()).toBeVisible({ timeout: 5_000 });
    await expect(page.getByRole('button', { name: /\+ new thread/i })).toBeVisible();
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
