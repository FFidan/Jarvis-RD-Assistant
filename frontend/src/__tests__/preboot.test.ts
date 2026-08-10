/**
 * Verify that preboot.js only applies allowlisted appearance
 * values to classList — arbitrary localStorage values must not be reflected.
 *
 * Strategy: read the raw script text, eval it inside the jsdom document
 * context, and assert classList state on document.documentElement.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const PREBOOT_PATH = path.resolve(__dirname, '../../public/preboot.js');
const prebootSrc = fs.readFileSync(PREBOOT_PATH, 'utf-8');

/**
 * Execute preboot.js after seeding localStorage, then return the classList
 * of document.documentElement so each test can make assertions.
 */
function runPreboot(appearance: Record<string, string> | null): DOMTokenList {
  // Reset html element classes and localStorage before each invocation.
  document.documentElement.className = '';
  localStorage.clear();

  if (appearance !== null) {
    localStorage.setItem('jarvis.appearance', JSON.stringify(appearance));
  }

  eval(prebootSrc); // intentional: exercising the raw script in a jsdom context

  return document.documentElement.classList;
}

describe('preboot.js — localStorage allowlist', () => {
  beforeEach(() => {
    document.documentElement.className = '';
    localStorage.clear();
  });

  // --- accent ---

  it('applies a valid accent class (forest)', () => {
    const classes = runPreboot({ accent: 'forest' });
    expect(classes.contains('accent-forest')).toBe(true);
  });

  it('applies a valid accent class (burgundy)', () => {
    const classes = runPreboot({ accent: 'burgundy' });
    expect(classes.contains('accent-burgundy')).toBe(true);
  });

  it('does NOT apply an invalid accent value', () => {
    const classes = runPreboot({ accent: 'xss; alert(1)' });
    // The raw injected string must not appear in classList at all.
    expect(classes.contains('accent-xss; alert(1)')).toBe(false);
    // No stray accent-* classes either.
    const accentClasses = [...classes].filter((c) => c.startsWith('accent-'));
    expect(accentClasses).toHaveLength(0);
  });

  it('does NOT apply an unknown accent value', () => {
    const classes = runPreboot({ accent: 'neon-pink' });
    const accentClasses = [...classes].filter((c) => c.startsWith('accent-'));
    expect(accentClasses).toHaveLength(0);
  });

  // ink-blue is the default — preboot skips adding it (avoids redundant class).
  it('does NOT add accent-ink-blue (default, skipped by design)', () => {
    const classes = runPreboot({ accent: 'ink-blue' });
    expect(classes.contains('accent-ink-blue')).toBe(false);
  });

  // --- type ---

  it('applies a valid type class (sans-modern)', () => {
    const classes = runPreboot({ type: 'sans-modern' });
    expect(classes.contains('type-sans-modern')).toBe(true);
  });

  it('does NOT apply a script-tag type value', () => {
    const classes = runPreboot({ type: '<script>' });
    expect(classes.contains('type-<script>')).toBe(false);
    const typeClasses = [...classes].filter((c) => c.startsWith('type-'));
    expect(typeClasses).toHaveLength(0);
  });

  // serif-calm is the default — preboot skips it.
  it('does NOT add type-serif-calm (default, skipped by design)', () => {
    const classes = runPreboot({ type: 'serif-calm' });
    expect(classes.contains('type-serif-calm')).toBe(false);
  });

  // --- density ---

  it('applies a valid density class (compact)', () => {
    const classes = runPreboot({ density: 'compact' });
    expect(classes.contains('density-compact')).toBe(true);
  });

  it('applies a valid density class (comfortable)', () => {
    const classes = runPreboot({ density: 'comfortable' });
    expect(classes.contains('density-comfortable')).toBe(true);
  });

  it('does NOT apply an unknown density value', () => {
    const classes = runPreboot({ density: 'ultra-wide' });
    const densityClasses = [...classes].filter((c) => c.startsWith('density-'));
    expect(densityClasses).toHaveLength(0);
  });

  // default density is skipped by design.
  it('does NOT add density-default (default, skipped by design)', () => {
    const classes = runPreboot({ density: 'default' });
    expect(classes.contains('density-default')).toBe(false);
  });

  // --- empty / null prefs ---

  it('adds no appearance classes when jarvis.appearance is absent', () => {
    const classes = runPreboot(null);
    const appearanceClasses = [...classes].filter(
      (c) => c.startsWith('accent-') || c.startsWith('type-') || c.startsWith('density-'),
    );
    expect(appearanceClasses).toHaveLength(0);
  });

  it('adds no appearance classes when prefs is an empty object', () => {
    const classes = runPreboot({});
    const appearanceClasses = [...classes].filter(
      (c) => c.startsWith('accent-') || c.startsWith('type-') || c.startsWith('density-'),
    );
    expect(appearanceClasses).toHaveLength(0);
  });

  it('disables Zod JIT before application modules load under the strict CSP', () => {
    runPreboot(null);

    expect(Reflect.get(globalThis, '__zod_globalConfig')).toMatchObject({ jitless: true });
  });
});
