import { describe, it, expect } from 'vitest';
import { escapeMarkdownInline } from './markdown-escape';

describe('escapeMarkdownInline', () => {
  it('escapes markdown special chars', () => {
    const escaped = escapeMarkdownInline('**bold** _italic_ [link](url)');
    expect(escaped).toBe('\\*\\*bold\\*\\* \\_italic\\_ \\[link\\]\\(url\\)');
  });

  it('escapes backticks and code-fences', () => {
    expect(escapeMarkdownInline('`code`')).toBe('\\`code\\`');
  });

  it('returns empty string unchanged', () => {
    expect(escapeMarkdownInline('')).toBe('');
  });

  it('escapeMarkdownInline is single-pass-only (calling twice double-escapes)', () => {
    const input = '\\*x*';
    const once = escapeMarkdownInline(input);
    const twice = escapeMarkdownInline(once);
    // Applying a second time re-escapes the already-escaped backslashes,
    // proving this function is NOT idempotent and must be called only once.
    expect(twice).not.toBe(once);
  });
});
