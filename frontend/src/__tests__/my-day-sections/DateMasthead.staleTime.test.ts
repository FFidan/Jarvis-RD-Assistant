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
