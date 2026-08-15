import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { ExternalLink, Loader2, MoreHorizontal, RefreshCw, Send } from 'lucide-react';
import { toast } from 'sonner';
import type { SearchPreviewResult } from '@/types';
import {
  zoteroGetLinkage,
  zoteroPushPaper,
  zoteroResync,
} from '@/lib/api';
import { zoteroDesktopHref, zoteroWebHref } from '@/lib/api/zotero';
import { useJobStore } from '@/stores/job-store';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { formatAuthors, formatDate } from '@/lib/utils';
import { SOURCE_LABELS } from '@/components/feed/source-labels';
import { errorMessage } from '@/lib/errors';

interface SearchPreviewRowProps {
  paper: SearchPreviewResult;
  selected: boolean;
  isSelectedDisabled: boolean;
  onToggleSelection: () => void;
  onPrimaryClick: () => void;
  onSavePaper: () => void;
  isSaving: boolean;
}

type ZoteroAction = 'push' | 'resync';

export function SearchPreviewRow({
  paper,
  selected,
  isSelectedDisabled,
  onToggleSelection,
  onPrimaryClick,
  onSavePaper,
  isSaving,
}: SearchPreviewRowProps) {
  const [pendingAction, setPendingAction] = useState<ZoteroAction | null>(null);
  const paperId = paper.library_match?.paper_id ?? null;
  const hasProjectLinks = paper.library_match?.has_project_links ?? false;
  const initialZoteroItemKey = paper.library_match?.zotero_item_key ?? null;
  const trackedZoteroJob = useJobStore((state) =>
    Object.values(state.jobs).find((job) => {
      if (job.kind !== 'zotero.push' && job.kind !== 'zotero.resync') return false;
      return job.payload?.paper_id === paperId;
    }) ?? null,
  );
  const activeJobExists =
    trackedZoteroJob?.status === 'queued' || trackedZoteroJob?.status === 'running';
  const [observeZoteroLinkage, setObserveZoteroLinkage] = useState(
    () => initialZoteroItemKey == null && trackedZoteroJob != null,
  );
  useEffect(() => {
    if (paperId == null || initialZoteroItemKey != null || trackedZoteroJob == null) return;
    setObserveZoteroLinkage(true);
  }, [initialZoteroItemKey, paperId, trackedZoteroJob]);

  const { data: savedZoteroLinkage } = useQuery({
    queryKey: QUERY_KEYS.zotero.linkage(paperId as number),
    queryFn: () => zoteroGetLinkage(paperId as number),
    enabled:
      paperId != null &&
      (initialZoteroItemKey != null || observeZoteroLinkage || trackedZoteroJob != null),
  });

  const zoteroItemKey =
    initialZoteroItemKey ?? savedZoteroLinkage?.zotero_item_key ?? null;
  const isSaved = paperId != null;
  const isSavedWithZotero = isSaved && zoteroItemKey != null;
  const isSavedWithoutProjects = isSaved && !isSavedWithZotero && !hasProjectLinks;
  const isSavedWithProjects = isSaved && !isSavedWithZotero && hasProjectLinks;
  const isZoteroBusy = pendingAction !== null || activeJobExists;
  const isValidUrl =
    paper.url && (paper.url.startsWith('http://') || paper.url.startsWith('https://'));

  async function handleZoteroAction(action: ZoteroAction) {
    if (paperId == null) return;

    setPendingAction(action);
    try {
      const response = action === 'push'
        ? await zoteroPushPaper(paperId)
        : await zoteroResync(paperId);

      if (initialZoteroItemKey == null) {
        setObserveZoteroLinkage(true);
      }
      useJobStore.getState().trackExternalJob({
        jobId: response.job_id,
        kind: action === 'push' ? 'zotero.push' : 'zotero.resync',
        payload: { paper_id: paperId },
        status: response.status as 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled',
      });
    } catch (error) {
      toast.error(errorMessage(error, 'Zotero action failed.'));
    } finally {
      setPendingAction(null);
    }
  }

  return (
    <div className="flex items-start gap-3 rounded-lg border p-3">
      <input
        type="checkbox"
        checked={selected}
        disabled={isSelectedDisabled}
        onChange={onToggleSelection}
        className="mt-1 h-4 w-4 rounded border-gray-300"
        aria-label={
          isSelectedDisabled
            ? `Already in library: ${paper.title || 'paper'}`
            : `Select ${paper.title || 'paper'}`
        }
      />

      <button
        type="button"
        onClick={onPrimaryClick}
        className="min-w-0 flex-1 rounded-md text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <div className="flex flex-wrap items-start gap-2">
          <p className="font-medium leading-tight flex-1">{paper.title || 'Untitled'}</p>
          {paper.source_type && (
            <Badge variant="outline" className="shrink-0 text-xs">
              {SOURCE_LABELS[paper.source_type] ?? paper.source_type}
            </Badge>
          )}
          {isSaved && (
            <Badge variant="secondary" className="shrink-0 text-xs">
              Already in library
            </Badge>
          )}
        </div>
        <p className="text-sm text-muted-foreground">
          {formatAuthors(paper.authors)} &middot; {formatDate(paper.published_date)}
        </p>
        {paper.abstract && (
          <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
            {paper.abstract}
          </p>
        )}
      </button>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="shrink-0"
            aria-label={`Actions for ${paper.title || 'paper'}`}
          >
            <MoreHorizontal className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          {!isSaved && (
            <DropdownMenuItem
              onSelect={(event) => {
                event.preventDefault();
                onSavePaper();
              }}
              disabled={isSaving}
            >
              {isSaving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              Save to your papers
            </DropdownMenuItem>
          )}

          {isSaved && (
            <DropdownMenuItem asChild>
              <Link to={`/paper/${paperId}`}>Open Paper Detail</Link>
            </DropdownMenuItem>
          )}

          {isValidUrl && (
            <DropdownMenuItem asChild>
              <a href={paper.url} target="_blank" rel="noopener noreferrer">
                Open original
                <ExternalLink className="ml-auto h-4 w-4" />
              </a>
            </DropdownMenuItem>
          )}

          {isSavedWithoutProjects && (
            <DropdownMenuItem asChild>
              <Link to="/projects">Open Projects to Link</Link>
            </DropdownMenuItem>
          )}

          {isSavedWithProjects && !isSavedWithZotero && (
            <DropdownMenuItem
              onSelect={(event) => {
                event.preventDefault();
                void handleZoteroAction('push');
              }}
              disabled={isZoteroBusy}
            >
              {pendingAction === 'push' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Send className="mr-2 h-4 w-4" />}
              Send to Zotero
            </DropdownMenuItem>
          )}

          {isSavedWithZotero && zoteroItemKey && (
            <DropdownMenuItem asChild>
              <a
                href={zoteroDesktopHref(
                  zoteroItemKey,
                  savedZoteroLinkage?.zotero_library_type,
                  savedZoteroLinkage?.zotero_group_id,
                )}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open in Zotero desktop
              </a>
            </DropdownMenuItem>
          )}

          {isSavedWithZotero && zoteroItemKey && (
            <DropdownMenuItem asChild>
              <a
                href={zoteroWebHref(
                  zoteroItemKey,
                  savedZoteroLinkage?.zotero_library_type,
                  savedZoteroLinkage?.zotero_group_id,
                )}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open Zotero Web Library
              </a>
            </DropdownMenuItem>
          )}

          {isSavedWithZotero && zoteroItemKey && (
            <DropdownMenuItem
              onSelect={(event) => {
                event.preventDefault();
                void handleZoteroAction('resync');
              }}
              disabled={isZoteroBusy}
            >
              {pendingAction === 'resync' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
              Re-sync Zotero
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
