// W4-CF7 NOTE: This test is intentionally structural (source-text regex) rather
// than behavioural. A full behavioural test would require either msw or
// vitest-fetch-mock to intercept React Query's network calls, verify a first
// fetch fires on mount, advance time < 60 s, and assert no second fetch.
// Neither dependency is present in this project's test setup.  The structural
// check below is accepted as a sufficient guard for the staleTime contract; a
// behavioural upgrade can be added if msw is introduced in the future.

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { it, expect } from 'vitest';

it('pulse query in DateMasthead has staleTime of 60_000', () => {
  const src = readFileSync(
    path.resolve(__dirname, '../../components/my-day/sections/DateMasthead.tsx'),
    'utf8',
  );
  expect(src).toMatch(/queryFn:\s*fetchPulseToday[\s\S]{0,100}staleTime:\s*60_000/);
});
