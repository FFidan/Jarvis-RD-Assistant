import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MarkdownContent } from '@/components/shared/MarkdownContent';

describe('MarkdownContent — XSS sanitization', () => {
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
    // Either outcome satisfies the XSS sanitization requirement.
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

  // data: URI tightening — only data:image/* is allowed.
  it('allows data:image/png;base64 src — renders <img> with the data URI', () => {
    const dataUri = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
    const { container } = render(
      <MarkdownContent>{`![png](${dataUri})`}</MarkdownContent>,
    );
    const img = container.querySelector('img');
    expect(img).not.toBeNull();
    expect(img?.getAttribute('src')).toBe(dataUri);
  });

  it('blocks data:text/html src — renders no <img> element', () => {
    const { container } = render(
      <MarkdownContent>{'![xss](data:text/html,<script>alert(1)</script>)'}</MarkdownContent>,
    );
    const img = container.querySelector('img');
    expect(img).toBeNull();
  });

  it('blocks bare data: (non-image) src — renders no <img> element', () => {
    const { container } = render(
      <MarkdownContent>{'![bare](data:application/javascript,alert(1))'}</MarkdownContent>,
    );
    const img = container.querySelector('img');
    expect(img).toBeNull();
  });
});

describe('MarkdownContent — LaTeX math rendering', () => {
  it('renders inline LaTeX with KaTeX class attributes intact', () => {
    const { container } = render(<MarkdownContent>{'$E=mc^2$'}</MarkdownContent>);
    // KaTeX emits elements with class="katex"; if sanitize ran after KaTeX it
    // would strip those classes, leaving unstyled plain text.
    expect(container.querySelector('.katex')).not.toBeNull();
  });
});
