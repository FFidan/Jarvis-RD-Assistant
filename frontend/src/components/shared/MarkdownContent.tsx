import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import 'katex/dist/katex.min.css';
import type { ReactNode } from 'react';

// Hardened sanitize schema: extends defaultSchema but restricts src protocol to
// allow only http, https, and data: URIs (fine-grained data: filtering is done
// in the img component override below — reject everything except data:image/*).
const sanitizeSchema = {
  ...defaultSchema,
  protocols: {
    ...defaultSchema.protocols,
    src: ['http', 'https', 'data'],
  },
};

// react-markdown's defaultUrlTransform blocks data: URIs (not in its safeProtocol
// list). Extend it to pass data:image/* through so the img component override can
// apply its own fine-grained regex check. data:image/* is XSS-safe because browsers
// parse it as binary image data, not as HTML or script.
function urlTransform(url: string, key: string): string {
  if (key === 'src' && /^data:image\//i.test(url)) return url;
  return defaultUrlTransform(url);
}

interface MarkdownContentProps {
  children: string;
  className?: string;
  unverifiedSentences?: string[];
}

/**
 * Walk a single React text node (string) and split it into alternating
 * plain-text / <mark> segments wherever any unverified sentence appears.
 *
 * Uses a single linear scan over the text to avoid O(n*m) regex compilation
 * per render.
 */
function highlightTextNode(text: string, unverified: string[], keyPrefix: string): ReactNode[] {
  if (unverified.length === 0) return [text];

  // Build a list of (start, end, sentence) matches in document order, then render.
  const matches: { start: number; end: number; sentence: string }[] = [];

  for (const sentence of unverified) {
    if (!sentence) continue;
    const idx = text.indexOf(sentence);
    if (idx === -1) continue;
    // Only add if it doesn't overlap with an already-found match.
    const overlaps = matches.some((m) => idx < m.end && idx + sentence.length > m.start);
    if (!overlaps) {
      matches.push({ start: idx, end: idx + sentence.length, sentence });
    }
  }

  if (matches.length === 0) return [text];

  // Sort by start position for a left-to-right scan.
  matches.sort((a, b) => a.start - b.start);

  const nodes: ReactNode[] = [];
  let cursor = 0;

  for (const match of matches) {
    if (cursor < match.start) {
      nodes.push(text.slice(cursor, match.start));
    }
    nodes.push(
      <mark
        key={`${keyPrefix}-${match.start}`}
        className="bg-yellow-100 text-yellow-900 rounded px-0.5"
      >
        {match.sentence}
      </mark>,
    );
    cursor = match.end;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }

  return nodes;
}

/**
 * Recursively walk React children, replacing string nodes that contain
 * unverified sentences with highlighted segments.
 */
function highlightChildren(
  children: ReactNode,
  unverified: string[],
  keyPrefix: string,
): ReactNode {
  if (typeof children === 'string') {
    const segments = highlightTextNode(children, unverified, keyPrefix);
    return segments.length === 1 && typeof segments[0] === 'string' ? segments[0] : segments;
  }
  if (Array.isArray(children)) {
    return children.map((child, i) => highlightChildren(child, unverified, `${keyPrefix}-${i}`));
  }
  return children;
}

export function MarkdownContent({ children, className, unverifiedSentences }: MarkdownContentProps) {
  const hasUnverified = unverifiedSentences && unverifiedSentences.length > 0;

  return (
    <ReactMarkdown
      className={className ?? 'prose prose-sm dark:prose-invert max-w-none'}
      remarkPlugins={[remarkMath]}
      rehypePlugins={[rehypeKatex, [rehypeSanitize, sanitizeSchema]]}
      urlTransform={urlTransform}
      components={{
        a: ({ node: _node, href, children, ...props }) => {
          const hrefLower = (href ?? '').toLowerCase().trimStart();
          const isDangerous =
            hrefLower.startsWith('javascript:') ||
            (hrefLower.startsWith('data:') && !hrefLower.startsWith('data:image/'));
          if (isDangerous) {
            return <span {...props}>{children}</span>;
          }
          return (
             
            <a href={href} {...props} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          );
        },
        img: ({ node: _node, src, alt, ...props }) => {
          // Allow only http/https and safe data:image/* URIs; reject everything else.
          const safe =
            typeof src === 'string' &&
            /^(https?:|data:image\/(png|jpe?g|gif|webp|svg\+xml);)/i.test(src);
          if (!safe) return null;
          return <img src={src} alt={alt ?? ''} loading="lazy" {...props} />;
        },
        ...(hasUnverified
          ? {
              p: ({ node: _node, children: pChildren, ...props }) => (
                <p {...props}>
                  {highlightChildren(pChildren as ReactNode, unverifiedSentences, 'mark')}
                </p>
              ),
            }
          : {}),
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
