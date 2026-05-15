/**
 * Cross-user isolation — SUPPLEMENTARY / scaffolding-grade. NOT a CI blocker.
 *
 * LIMITATION (read before trusting this file):
 * The production auth model for browser sessions is magic-link → a
 * `jarvis_session` cookie validated server-side. The existing e2e harness,
 * however, gates on an API *key* persisted in `sessionStorage['jarvis-auth']`
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
import { expect, test } from '@playwright/test';

const FEED_PATH = '/';

/** Seed a distinct auth state into a context's first page load. */
async function seedDistinctAuth(context, apiKey: string): Promise<void> {
  await context.addInitScript((key: string) => {
    const state = {
      state: { isAuthenticated: true, authTime: Date.now(), apiKey: key },
      version: 0,
    };
    window.sessionStorage.setItem('jarvis-auth', JSON.stringify(state));
  }, apiKey);
}

test.describe('@cross-user two-context data isolation (scaffolding)', () => {
  test('two seeded contexts render independently without auth bleed', async ({
    browser,
  }) => {
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    try {
      await seedDistinctAuth(ctxA, 'isolation-key-user-a');
      await seedDistinctAuth(ctxB, 'isolation-key-user-b');

      const pageA = await ctxA.newPage();
      const pageB = await ctxB.newPage();

      await pageA.goto(FEED_PATH);
      await pageB.goto(FEED_PATH);

      // Each context must hold ONLY its own seeded credential — no bleed
      // through shared storage. This part is a hard assertion.
      const keyA = await pageA.evaluate(() =>
        JSON.parse(window.sessionStorage.getItem('jarvis-auth') ?? '{}')?.state
          ?.apiKey,
      );
      const keyB = await pageB.evaluate(() =>
        JSON.parse(window.sessionStorage.getItem('jarvis-auth') ?? '{}')?.state
          ?.apiKey,
      );
      expect(keyA).toBe('isolation-key-user-a');
      expect(keyB).toBe('isolation-key-user-b');
      expect(keyA).not.toBe(keyB);

      // Content disjointness is only meaningful against a per-user live
      // backend. Against the mocked backend both see identical fixtures, so
      // this is observational, not a gate.
      const bodyA = (await pageA.locator('body').textContent()) ?? '';
      const bodyB = (await pageB.locator('body').textContent()) ?? '';
      if (bodyA && bodyB && bodyA !== bodyB) {
        // eslint-disable-next-line no-console
        console.log('[cross-user] contexts rendered distinct content (good).');
      } else {
        // eslint-disable-next-line no-console
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
