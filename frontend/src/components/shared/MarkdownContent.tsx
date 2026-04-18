import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';

interface MarkdownContentProps {
  children: string;
  className?: string;
}

export function MarkdownContent({ children, className }: MarkdownContentProps) {
  return (
    <ReactMarkdown
      className={className ?? 'prose prose-sm dark:prose-invert max-w-none'}
      remarkPlugins={[remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={{
        a: ({ node: _node, ...props }) => (
          // eslint-disable-next-line react/jsx-no-target-blank
          <a {...props} target="_blank" rel="noopener noreferrer" />
        ),
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
