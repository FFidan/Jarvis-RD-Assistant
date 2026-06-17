import { defineConfig } from '@playwright/test';

const baseURL = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3001';

export default defineConfig({
  testDir: './e2e',
  use: {
    baseURL,
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    // Mocked specs stub the API via page.route(); the app's Service Worker
    // (public/sw.js) SAFELISTs read endpoints (feed, /papers/{id}, stats, …) and
    // would intercept those fetches before Playwright, hitting the real backend.
    // Block the SW so route stubs are authoritative and deterministic.
    serviceWorkers: 'block',
  },
  timeout: 30000,
  retries: 1,
});
