import { useState, useRef, useEffect } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Button } from '@/components/ui/button';
import {
  ApiError,
  zoteroGetLinkage,
  zoteroPushPaper,
  zoteroResync,
} from '@/lib/api';
import { zoteroDesktopHref, zoteroWebHref } from '@/lib/api/zotero';
import { useJobStore } from '@/stores/job-store';
import { Copy, ExternalLink, RefreshCw, Send } from 'lucide-react';

interface ZoteroPanelProps {
  paperId: number;
  hasProjectLinks: boolean;
}

export function ZoteroPanel({ paperId, hasProjectLinks }: ZoteroPanelProps) {
  const trackExternalJob = useJobStore((state) => state.trackExternalJob);
  const pushRunning = useJobStore((state) =>
    state.isRunning('zotero.push', { paper_id: paperId }),
  );
  const resyncRunning = useJobStore((state) =>
    state.isRunning('zotero.resync', { paper_id: paperId }),
  );
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'error'>('idle');
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
  }, []);

  const { data: linkage, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.zotero.linkage(paperId),
    queryFn: () => zoteroGetLinkage(paperId),
  });

  const pushMutation = useMutation({
    mutationFn: () => zoteroPushPaper(paperId),
    onSuccess: ({ job_id }) => trackExternalJob({
      jobId: job_id,
      kind: 'zotero.push',
      payload: { paper_id: paperId },
      status: 'queued',
    }),
  });

  const resyncMutation = useMutation({
    mutationFn: () => zoteroResync(paperId),
    onSuccess: ({ job_id }) => trackExternalJob({
      jobId: job_id,
      kind: 'zotero.resync',
      payload: { paper_id: paperId },
      status: 'queued',
    }),
  });

  const copyKey = async (key: string) => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    try {
      await navigator.clipboard.writeText(key);
      setCopyState('copied');
    } catch {
      setCopyState('error');
    }
    copyTimerRef.current = setTimeout(() => setCopyState('idle'), 2000);
  };

  if (isLoading) return <div className="text-sm text-muted-foreground">Loading Zotero status…</div>;
  if (isError) {
    const isPermissionError = error instanceof ApiError && error.status === 403;
    return (
      <div className="space-y-3">
        <h3 className="text-sm font-semibold">Zotero</h3>
        <p className="text-xs text-destructive">
          {isPermissionError
            ? "You don't have permission to view Zotero status for this paper."
            : 'Zotero status is temporarily unavailable. Try again shortly.'}
        </p>
      </div>
    );
  }

  const isPushed = !!linkage?.zotero_item_key;
  const pushing = pushMutation.isPending || pushRunning;
  const resyncing = resyncMutation.isPending || resyncRunning;

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold">Zotero</h3>
      {!isPushed ? (
        <div className="space-y-2">
          {!hasProjectLinks && (
            <p className="text-xs text-muted-foreground">
              Link this paper to a project first. The project determines its Zotero collection.
            </p>
          )}
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="outline"
              disabled={!hasProjectLinks || pushing}
              onClick={() => pushMutation.mutate()}
            >
              <Send className="h-3 w-3 mr-1" />
              {pushing ? 'Sending…' : 'Send to Zotero'}
            </Button>
            {!hasProjectLinks && (
              <Button size="sm" variant="outline" asChild>
                <Link to="/projects">Open Projects to Link</Link>
              </Button>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            Sends citation metadata first (the PDF is not attached). You can then synchronize
            annotations and highlights.
          </p>
          <a href="/settings?section=integrations&item=zotero" className="text-xs text-primary underline">
            Configure Zotero in Settings
          </a>
          {pushMutation.isError && <p className="text-xs text-destructive">Push failed. Try again.</p>}
        </div>
      ) : (
        <div className="space-y-2">
          {linkage.zotero_citation_key && (
            <div className="flex items-center gap-1">
              <code className="text-xs bg-muted px-1 py-0.5 rounded">{linkage.zotero_citation_key}</code>
              <Button size="icon" variant="ghost" className="h-5 w-5" onClick={() => copyKey(linkage.zotero_citation_key!)} aria-label="Copy citation key">
                <Copy className="h-3 w-3" />
              </Button>
              {copyState === 'copied' && <span className="text-xs text-muted-foreground">Copied!</span>}
              {copyState === 'error' && <span className="text-xs text-destructive">Copy failed</span>}
            </div>
          )}
          {!linkage.zotero_citation_key && (
            <p className="text-xs text-muted-foreground">
              Item key: <code>{linkage.zotero_item_key}</code>
              <span className="ml-1" title="Install Better BibTeX for citation keys">(BBT not found)</span>
            </p>
          )}
          <div className="flex gap-1">
            <Button size="sm" variant="outline" asChild>
              <a
                href={zoteroDesktopHref(
                  linkage.zotero_item_key!,
                  linkage.zotero_library_type,
                  linkage.zotero_group_id,
                )}
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLink className="h-3 w-3 mr-1" />
                Open in Zotero desktop
              </a>
            </Button>
            <Button size="sm" variant="outline" asChild>
              <a
                href={zoteroWebHref(
                  linkage.zotero_item_key!,
                  linkage.zotero_library_type,
                  linkage.zotero_group_id,
                )}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open Zotero Web Library
              </a>
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={resyncing}
              onClick={() => resyncMutation.mutate()}
              title="Re-push to Zotero"
              aria-label="Re-push to Zotero"
            >
              <RefreshCw className={`h-3 w-3 ${resyncing ? 'animate-spin' : ''}`} />
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            This item is filed with its linked project collection. Citation metadata is sent before
            annotations and highlights are synchronized.
          </p>
        </div>
      )}
    </div>
  );
}
