import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
import type { ReactNode } from 'react';

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
      rehypePlugins={[rehypeKatex]}
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
            // eslint-disable-next-line react/jsx-no-target-blank
            <a href={href} {...props} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          );
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
