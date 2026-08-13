/**
 * Cross-user isolation — SUPPLEMENTARY / scaffolding-grade. NOT a CI blocker.
 *
 * LIMITATION (read before trusting this file):
 * The production auth model for browser sessions is magic-link → a
 * `jarvis_session` cookie validated server-side. The existing e2e harness,
 * however, gates on distinct cookie-session identities persisted in session storage
 * (see `e2e/helpers/setup.ts::seedAuthedSession`) and the mocked suite stubs
 * the backend entirely. A faithful two-real-user browser test would require
 * a live backend with two provisioned magic-link sessions — high friction and
 * out of scope for the mocked harness.
 *
 * This spec therefore only demonstrates the *shape* of a two-context
 * isolation check: two independent browser contexts, two DISTINCT seeded
 * auth states, each navigated to the feed, asserting their visible content
 * is disjoint IF the backend serves per-user data. Against the mocked
 * backend both contexts see identical fixture data, so the disjointness
 * assertion is intentionally soft (logged, not failed). The authoritative
 * isolation gate is the Python suite
 * `services/paper_ingestion/tests/integration/test_cross_user_isolation.py`.
 *
 * Tagged `@cross-user`; excluded from `test:e2e:mocked` (explicit file list)
 * and runnable only via `npm --prefix frontend run test:e2e:cross-user`.
 */
import { expect, test, type BrowserContext } from '@playwright/test';

const FEED_PATH = '/';

/** Seed a distinct auth state into a context's first page load. */
async function seedDistinctAuth(
  context: BrowserContext,
  user: { id: number; email: string; role: 'user' },
): Promise<void> {
  await context.addInitScript((identity) => {
    const state = {
      state: { isAuthenticated: true, authTime: Date.now(), user: identity },
      version: 0,
    };
    window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
  }, user);
}

test.describe('@cross-user two-context data isolation (scaffolding)', () => {
  test('two seeded contexts render independently without auth bleed', async ({
    browser,
  }) => {
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    try {
      await seedDistinctAuth(ctxA, { id: 1, email: 'a@example.test', role: 'user' });
      await seedDistinctAuth(ctxB, { id: 2, email: 'b@example.test', role: 'user' });

      const pageA = await ctxA.newPage();
      const pageB = await ctxB.newPage();

      await pageA.goto(FEED_PATH);
      await pageB.goto(FEED_PATH);

      // Each context must hold only its own seeded identity — no bleed
      // through shared storage. This part is a hard assertion.
      const userA = await pageA.evaluate(() =>
        JSON.parse(window.sessionStorage.getItem('jarvis-auth') ?? '{}')?.state
          ?.user,
      );
      const userB = await pageB.evaluate(() =>
        JSON.parse(window.sessionStorage.getItem('jarvis-auth') ?? '{}')?.state
          ?.user,
      );
      expect(userA?.id).toBe(1);
      expect(userB?.id).toBe(2);
      expect(userA).not.toEqual(userB);

      // Content disjointness is only meaningful against a per-user live
      // backend. Against the mocked backend both see identical fixtures, so
      // this is observational, not a gate.
      const bodyA = (await pageA.locator('body').textContent()) ?? '';
      const bodyB = (await pageB.locator('body').textContent()) ?? '';
      if (bodyA && bodyB && bodyA !== bodyB) {
         
        console.log('[cross-user] contexts rendered distinct content (good).');
      } else {
         
        console.log(
          '[cross-user] identical content — expected against mocked backend; ' +
            'authoritative gate is the Python integration suite.',
        );
      }
    } finally {
      await ctxA.close();
      await ctxB.close();
    }
  });
});
