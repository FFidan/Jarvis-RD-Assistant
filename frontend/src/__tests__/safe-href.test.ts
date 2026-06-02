import { describe, it, expect } from 'vitest';
import { isSafeRelativeHref } from '@/lib/safe-href';

describe('isSafeRelativeHref', () => {
  it('accepts app-relative paths beginning with a single /', () => {
    expect(isSafeRelativeHref('/paper/1')).toBe(true);
    expect(isSafeRelativeHref('/paper/1?action=process')).toBe(true);
    expect(isSafeRelativeHref('/')).toBe(true);
  });

  it('rejects javascript: URIs (any case/whitespace)', () => {
    expect(isSafeRelativeHref('javascript:alert(1)')).toBe(false);
    expect(isSafeRelativeHref('JavaScript:alert(1)')).toBe(false);
    expect(isSafeRelativeHref('  javascript:alert(1)')).toBe(false);
    expect(isSafeRelativeHref('java\tscript:alert(1)')).toBe(false);
  });

  it('rejects protocol-relative // URLs', () => {
    expect(isSafeRelativeHref('//evil.com')).toBe(false);
    expect(isSafeRelativeHref('//evil.com/path')).toBe(false);
  });

  it('rejects absolute scheme URLs', () => {
    expect(isSafeRelativeHref('https://x')).toBe(false);
    expect(isSafeRelativeHref('http://x')).toBe(false);
    expect(isSafeRelativeHref('data:text/html,x')).toBe(false);
    expect(isSafeRelativeHref('mailto:a@b.c')).toBe(false);
  });

  it('rejects backslash tricks', () => {
    expect(isSafeRelativeHref('\\evil')).toBe(false);
    expect(isSafeRelativeHref('\\\\evil')).toBe(false);
    expect(isSafeRelativeHref('/\\evil')).toBe(false);
  });

  it('rejects empty / non-string / relative-without-leading-slash', () => {
    expect(isSafeRelativeHref('')).toBe(false);
    expect(isSafeRelativeHref('paper/1')).toBe(false);
    // @ts-expect-error — defends against non-string at runtime
    expect(isSafeRelativeHref(null)).toBe(false);
    // @ts-expect-error — defends against non-string at runtime
    expect(isSafeRelativeHref(undefined)).toBe(false);
  });
});
