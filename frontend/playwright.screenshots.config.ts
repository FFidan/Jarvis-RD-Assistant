import { defineConfig } from '@playwright/test';

import baseConfig from './playwright.config';

// The README screenshot generator is documentation tooling, not a test: it has
// no assertions and it overwrites the tracked PNGs in docs/screenshots/. Keeping
// it out of the e2e testDir stops `npm run test:e2e` from rewriting tracked
// files, so it needs its own testDir to stay runnable on demand. Everything else
// (baseURL, timeout, service-worker blocking) is inherited so the generator and
// the e2e suite cannot drift apart.
export default defineConfig({
  ...baseConfig,
  testDir: './scripts',
  testMatch: 'generate-screenshots.ts',
});
