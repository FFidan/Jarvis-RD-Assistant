import { useState, useRef, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Button } from '@/components/ui/button';
import { zoteroPushPaper, zoteroGetLinkage, zoteroResync } from '@/lib/api';
import { Copy, ExternalLink, RefreshCw, Send } from 'lucide-react';

interface ZoteroPanelProps {
  paperId: number;
  hasProjectLinks: boolean;
}

export function ZoteroPanel({ paperId, hasProjectLinks }: ZoteroPanelProps) {
  const queryClient = useQueryClient();
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'error'>('idle');
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
  }, []);

  const { data: linkage, isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.zotero.linkage(paperId),
    queryFn: () => zoteroGetLinkage(paperId),
  });

  const pushMutation = useMutation({
    mutationFn: () => zoteroPushPaper(paperId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.zotero.linkage(paperId) }),
  });

  const resyncMutation = useMutation({
    mutationFn: () => zoteroResync(paperId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.zotero.linkage(paperId) }),
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
    return (
      <div className="space-y-3">
        <h3 className="text-sm font-semibold">Zotero</h3>
        <p className="text-xs text-destructive">Zotero status unavailable.</p>
      </div>
    );
  }

  const isPushed = !!linkage?.zotero_item_key;

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold">Zotero</h3>
      {!isPushed ? (
        <div className="space-y-2">
          {!hasProjectLinks && (
            <p className="text-xs text-muted-foreground">Link to a project first to enable Zotero push.</p>
          )}
          <Button
            size="sm"
            variant="outline"
            disabled={!hasProjectLinks || pushMutation.isPending}
            onClick={() => pushMutation.mutate()}
          >
            <Send className="h-3 w-3 mr-1" />
            {pushMutation.isPending ? 'Sending…' : 'Send to Zotero'}
          </Button>
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
              <a href={`zotero://select/library/items/${linkage.zotero_item_key}`} target="_blank" rel="noopener noreferrer">
                <ExternalLink className="h-3 w-3 mr-1" />
                View in Zotero
              </a>
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={resyncMutation.isPending}
              onClick={() => resyncMutation.mutate()}
              title="Re-push to Zotero"
            >
              <RefreshCw className={`h-3 w-3 ${resyncMutation.isPending ? 'animate-spin' : ''}`} />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
