import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

const srcRoot = path.resolve(__dirname, '..');
const apiRoot = path.join(srcRoot, 'lib/api');

function filesUnder(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolute = path.join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(absolute) : [absolute];
  }).filter((file) => file.endsWith('.ts') || file.endsWith('.tsx'));
}

function source(file: string): string {
  return readFileSync(file, 'utf8');
}

describe('frontend API source policy', () => {
  it('has no generic assertion-only JSON fetch escape hatch', () => {
    const apiFiles = filesUnder(apiRoot);
    const combined = apiFiles.map(source).join('\n');
    const core = source(path.join(apiRoot, 'core.ts'));
    const barrel = source(path.join(apiRoot, 'index.ts'));

    expect(combined).not.toMatch(/\bapiFetch\s*</);
    expect(core).not.toMatch(/export\s+(?:async\s+)?function\s+apiFetch\b/);
    expect(barrel).not.toMatch(/\bapiFetch\s*,/);
  });

  it('keeps successful response JSON parsing inside the decoded core boundary', () => {
    const guardedFiles = [
      ...filesUnder(apiRoot).filter((file) => !file.endsWith('/core.ts')),
      path.join(srcRoot, 'lib/logs.ts'),
      path.join(srcRoot, 'lib/review-outbox.ts'),
      path.join(srcRoot, 'lib/sse.ts'),
      path.join(srcRoot, 'stores/auth-store.ts'),
      path.join(srcRoot, 'components/settings/PulseSection.tsx'),
    ];

    for (const file of guardedFiles) {
      expect(source(file), path.relative(srcRoot, file)).not.toMatch(/\.json\s*\(\s*\)/);
    }
  });

  it('forbids unbounded schemas and double-cast decoder bypasses', () => {
    const guarded = [
      ...filesUnder(apiRoot),
      path.join(srcRoot, 'lib/logs.ts'),
      path.join(srcRoot, 'lib/review-outbox.ts'),
      path.join(srcRoot, 'lib/sse.ts'),
      path.join(srcRoot, 'stores/auth-store.ts'),
      path.join(srcRoot, 'stores/job-store.ts'),
    ].map(source).join('\n');

    expect(guarded).not.toMatch(/z\.(?:any|unknown)\s*\(/);
    expect(guarded).not.toMatch(/\bas\s+unknown\s+as\b/);
    expect(guarded).not.toMatch(/\bas\s+T\b/);
    expect(guarded).not.toMatch(/JSON\.parse\([^)]*\)\s+as\b/);
  });

  it('keeps raw API keys out of browser state and shared request clients', () => {
    const authStore = source(path.join(srcRoot, 'stores/auth-store.ts'));
    const sharedClients = [
      path.join(apiRoot, 'core.ts'),
      path.join(srcRoot, 'lib/sse.ts'),
      path.join(srcRoot, 'lib/logs.ts'),
      path.join(srcRoot, 'stores/job-store.ts'),
    ].map(source).join('\n');

    expect(authStore).not.toContain(['get', 'ApiKey'].join(''));
    expect(authStore).toContain('/api/auth/api-key-session');
    expect(authStore).toContain("credentials: 'include'");
    expect(sharedClients).not.toContain(['X', 'API-Key'].join('-'));
  });

  it('keeps short-lived auth tokens fragment-only', () => {
    const tokenConsumers = [
      path.join(srcRoot, 'pages/AuthVerifyPage.tsx'),
      path.join(srcRoot, 'components/settings/AccountSection.tsx'),
    ];

    for (const file of tokenConsumers) {
      const contents = source(file);
      expect(contents, path.relative(srcRoot, file)).toContain('location.hash');
      expect(contents, path.relative(srcRoot, file)).not.toMatch(
        /URLSearchParams\s*\(\s*location\.search\s*\)/,
      );
    }
  });

  it('keeps paper API callers on the current array-only surface', () => {
    const papers = source(path.join(apiRoot, 'papers.ts'));

    expect(papers).not.toContain(['fetchFeedCounts', 'WithFacets'].join(''));
    expect(papers).toMatch(
      /searchPreview\s*=\s*\(\s*query:\s*string,\s*sourceTypes\?:\s*string\[\],/s,
    );
  });
});
