import type { SearchPreviewResult } from '@/types';
import { useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { formatAuthors, formatDate } from '@/lib/utils';
import { SOURCE_LABELS } from '@/components/feed/source-labels';

interface SearchPreviewDrawerProps {
  paper: SearchPreviewResult | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: (papers: SearchPreviewResult[]) => void;
  isSaving: boolean;
}

export function SearchPreviewDrawer({
  paper,
  open,
  onOpenChange,
  onSave,
  isSaving,
}: SearchPreviewDrawerProps) {
  const navigate = useNavigate();

  if (!paper) {
    return null;
  }

  const sourceLabel = SOURCE_LABELS[paper.source_type] ?? paper.source_type;
  const isSaved = paper.library_match?.paper_id != null;
  const isValidUrl =
    paper.url && (paper.url.startsWith('http://') || paper.url.startsWith('https://'));

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="sm:max-w-xl">
        <SheetHeader className="space-y-3 text-left">
          <SheetTitle>{paper.title || 'Untitled'}</SheetTitle>
          <SheetDescription className="text-sm">
            {formatAuthors(paper.authors)} · {sourceLabel} · {formatDate(paper.published_date)}
          </SheetDescription>
        </SheetHeader>

        <div className="mt-6 space-y-4">
          <div className="space-y-1">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Abstract</p>
            <p className="text-sm leading-6 text-foreground">
              {paper.abstract ?? 'No abstract available.'}
            </p>
          </div>

          {paper.citation_count !== null && (
            <div className="space-y-1">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Citations</p>
              <p className="text-sm text-foreground">{paper.citation_count.toLocaleString()}</p>
            </div>
          )}

          <div className="flex flex-wrap gap-3 pt-2">
            <Button
              onClick={() => {
                if (isSaved && paper.library_match?.paper_id) {
                  navigate(`/paper/${paper.library_match.paper_id}`);
                  return;
                }
                onSave([paper]);
              }}
              disabled={!isSaved && isSaving}
            >
              {isSaved ? 'Open Paper Detail' : 'Save to Library'}
            </Button>
            {isValidUrl && (
              <Button variant="outline" asChild>
                <a href={paper.url} target="_blank" rel="noreferrer">
                  Open original
                </a>
              </Button>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
