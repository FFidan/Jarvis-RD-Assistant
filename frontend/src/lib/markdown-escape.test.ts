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
});
