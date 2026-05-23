import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MarkdownContent } from '@/components/shared/MarkdownContent';

describe('MarkdownContent — XSS sanitization (SEC-XSS-002)', () => {
  it('blocks javascript: img src — renders no <img> element', () => {
    const { container } = render(
      <MarkdownContent>{'![alt](javascript:alert(1))'}</MarkdownContent>,
    );
    const img = container.querySelector('img');
    // The img component override returns null for dangerous URIs.
    expect(img).toBeNull();
  });

  it('strips onerror event handler from inline <img> tag', () => {
    const { container } = render(
      // Inline HTML: rehype-sanitize must strip event-handler attributes.
      <MarkdownContent>{'<img src="x" onerror="fetch(\'//evil\')">'}</MarkdownContent>,
    );
    const img = container.querySelector('img');
    if (img) {
      // If an img survives, it must not carry the onerror attribute.
      expect(img.getAttribute('onerror')).toBeNull();
    }
    // It's also acceptable for the img to be removed entirely (safe fallback).
    // Either outcome satisfies SEC-XSS-002.
  });

  it('strips onclick from inline <a> tag', () => {
    const { container } = render(
      <MarkdownContent>{'<a href="https://example.com" onclick="x()">link</a>'}</MarkdownContent>,
    );
    const anchor = container.querySelector('a');
    if (anchor) {
      expect(anchor.getAttribute('onclick')).toBeNull();
    }
  });

  it('renders a safe https img correctly', () => {
    const { container } = render(
      <MarkdownContent>{'![ok](https://example.com/x.png)'}</MarkdownContent>,
    );
    const img = container.querySelector('img');
    expect(img).not.toBeNull();
    expect(img?.getAttribute('src')).toBe('https://example.com/x.png');
    expect(img?.getAttribute('alt')).toBe('ok');
  });
});
