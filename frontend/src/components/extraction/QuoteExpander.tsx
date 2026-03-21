import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';

interface QuoteExpanderProps {
  quote: string | null;
  pageNumber: number | null;
  verified: boolean;
}

export function QuoteExpander({ quote, pageNumber, verified }: QuoteExpanderProps) {
  const [expanded, setExpanded] = useState(false);

  if (!quote) {
    return <span className="text-muted-foreground">--</span>;
  }

  return (
    <div>
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        {expanded ? 'Hide quote' : 'Show quote'}
      </button>
      {expanded && (
        <div className="mt-1 rounded border-l-2 border-muted-foreground/30 bg-muted/50 px-3 py-2 text-xs">
          <blockquote className="italic">{quote}</blockquote>
          <div className="mt-1 flex gap-2 text-muted-foreground">
            {pageNumber && <span>Page {pageNumber}</span>}
            <span>{verified ? 'Verified' : 'Unverified'}</span>
          </div>
        </div>
      )}
    </div>
  );
}
