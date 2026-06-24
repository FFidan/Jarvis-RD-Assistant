import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronDown, ChevronUp } from 'lucide-react';
import type { Source } from '@/types';
import { Button } from '@/components/ui/button';
import { MarkdownContent } from '@/components/shared/MarkdownContent';

interface SourcesAccordionProps {
  sources: Source[];
}

export function SourcesAccordion({ sources }: SourcesAccordionProps) {
  const [open, setOpen] = useState(false);

  if (sources.length === 0) return null;

  return (
    <div className="mt-2 rounded-md border">
      <Button
        variant="ghost"
        size="sm"
        className="w-full justify-between"
        onClick={() => setOpen(!open)}
      >
        <span className="text-xs font-medium">
          {sources.length} source{sources.length !== 1 ? 's' : ''}
        </span>
        {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </Button>
      {open && (
        <div className="space-y-2 p-3 pt-0">
          {sources.map((source, i) => (
            <div key={source.chunk_id ?? `src-${i}`} className="rounded border p-2 text-xs">
              {source.paper_title && (
                <p className="font-medium">
                  {source.paper_id != null ? (
                    <Link to={`/paper/${source.paper_id}`} className="hover:underline">
                      {source.paper_title}
                    </Link>
                  ) : (
                    source.paper_title
                  )}
                </p>
              )}
              <div className="mt-1 text-muted-foreground line-clamp-3">
                <MarkdownContent className="prose prose-xs dark:prose-invert max-w-none text-muted-foreground">{source.text || source.content || ''}</MarkdownContent>
              </div>
              {source.page_number != null && (
                <p className="mt-1 text-muted-foreground">p.{source.page_number}</p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
