import { test, expect } from '@playwright/test';
import { seedAuthedSession } from './helpers/setup';

/**
 * Layout-level Pomodoro timer.
 *
 * The HeaderPomodoro component renders a mini timer in the TopBar
 * whenever `phase !== 'idle'`. We seed the pomodoro-store directly
 * (simpler than driving the Start Focus button and waiting on
 * setInterval ticks) and confirm:
 *   1. Timer pill is visible on My Day
 *   2. Timer pill remains visible across route changes
 *   3. The Pause button toggles between pause and resume icons
 */
test.describe('Pomodoro timer in AppShell @pomodoro', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthedSession(page);

    // Seed the pomodoro-store into localStorage so HeaderPomodoro
    // renders immediately in the non-idle state.
    await page.addInitScript(() => {
      const state = {
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
          attachedItem: null,
          completedSession: null,
        },
        version: 0,
      };
      window.localStorage.setItem('jarvis-pomodoro', JSON.stringify(state));
    });
  });

  test('timer pill is visible across route changes and pause toggles', async ({ page }) => {
    await page.goto('/my-day');

    // HeaderPomodoro renders MM:SS text — assert some value is shown.
    const timerButton = page.getByRole('button', { name: /pomodoro focus/i });
    await expect(timerButton).toBeVisible({ timeout: 10_000 });

    const pauseButton = page.getByRole('button', { name: /pause pomodoro/i });
    await expect(pauseButton).toBeVisible();

    // Navigate to Feed — timer pill should still be visible.
    await page.getByRole('link', { name: 'Feed' }).click();
    await expect(page).toHaveURL(/\/feed/);
    await expect(timerButton).toBeVisible({ timeout: 5_000 });

    // Navigate to Settings — still visible.
    await page.getByRole('link', { name: /settings/i }).click();
    await expect(page).toHaveURL(/\/settings/);
    await expect(timerButton).toBeVisible({ timeout: 5_000 });

    // Click pause — aria-label flips to "Resume Pomodoro".
    await pauseButton.click();
    await expect(page.getByRole('button', { name: /resume pomodoro/i })).toBeVisible({
      timeout: 5_000,
    });

    // Click resume — back to pause.
    await page.getByRole('button', { name: /resume pomodoro/i }).click();
    await expect(page.getByRole('button', { name: /pause pomodoro/i })).toBeVisible({
      timeout: 5_000,
    });
  });
});
