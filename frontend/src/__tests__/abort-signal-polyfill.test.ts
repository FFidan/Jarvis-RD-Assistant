/**
 * FE-004: Unit tests for the AbortSignal.any polyfill.
 *
 * Verifies that applyAbortSignalAnyPolyfill installs AbortSignal.any when
 * the native implementation is absent, and that the installed shim behaves
 * correctly for the core usage patterns.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { applyAbortSignalAnyPolyfill } from '@/lib/abort-signal-polyfill';

describe('AbortSignal.any polyfill (FE-004)', () => {
  // Save the original before each test so we can restore / re-delete it.
  let originalAny: typeof AbortSignal.any | undefined;

  beforeEach(() => {
    originalAny = (AbortSignal as { any?: typeof AbortSignal.any }).any;
  });

  afterEach(() => {
    // Restore whatever was there before.
    (AbortSignal as { any?: typeof AbortSignal.any }).any = originalAny;
  });

  it('installs AbortSignal.any when the native method is absent', () => {
    // Delete the native/previously polyfilled version.
    delete (AbortSignal as { any?: unknown }).any;
    expect(typeof AbortSignal.any).toBe('undefined');

    applyAbortSignalAnyPolyfill();

    expect(typeof AbortSignal.any).toBe('function');
  });

  it('does not overwrite a pre-existing AbortSignal.any', () => {
    // Ensure something is already installed (native or from setup.ts).
    if (typeof AbortSignal.any !== 'function') {
      applyAbortSignalAnyPolyfill();
    }
    const existingAny = AbortSignal.any;

    applyAbortSignalAnyPolyfill(); // second call — must be a no-op.

    expect(AbortSignal.any).toBe(existingAny);
  });

  it('polyfill returns an AbortSignal', () => {
    delete (AbortSignal as { any?: unknown }).any;
    applyAbortSignalAnyPolyfill();

    const ctrl = new AbortController();
    const result = AbortSignal.any([ctrl.signal]);
    expect(result).toBeInstanceOf(AbortSignal);
  });

  it('polyfill signal aborts when one of its input signals aborts', () => {
    delete (AbortSignal as { any?: unknown }).any;
    applyAbortSignalAnyPolyfill();

    const ctrl1 = new AbortController();
    const ctrl2 = new AbortController();
    const combined = AbortSignal.any([ctrl1.signal, ctrl2.signal]);

    expect(combined.aborted).toBe(false);
    ctrl1.abort('test-reason');
    expect(combined.aborted).toBe(true);
  });

  it('polyfill signal is immediately aborted if an input is already aborted', () => {
    delete (AbortSignal as { any?: unknown }).any;
    applyAbortSignalAnyPolyfill();

    const ctrl = new AbortController();
    ctrl.abort('already-aborted');

    const combined = AbortSignal.any([ctrl.signal]);
    expect(combined.aborted).toBe(true);
  });

  it('polyfill works with an empty signals array (never aborts)', () => {
    delete (AbortSignal as { any?: unknown }).any;
    applyAbortSignalAnyPolyfill();

    const result = AbortSignal.any([]);
    expect(result.aborted).toBe(false);
  });
});
