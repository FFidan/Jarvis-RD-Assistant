import { useEffect, useRef, useState } from 'react';
import { fetchSnapshot } from '@/lib/api';
import { Skeleton } from '@/components/ui/skeleton';
import { ImageOff } from 'lucide-react';

interface EvidenceSnapshotProps {
  paperId: number;
  page: number;
  altText?: string;
  variant?: 'thumbnail' | 'full';
}

type State = 'loading' | 'ok' | 'error';

/**
 * Renders a PDF page snapshot served from /api/snapshots/{paperId}/{page}.
 *
 * Auth constraint: the snapshot endpoint requires X-API-Key, which native
 * <img> requests don't send. We use apiFetchRaw → blob → createObjectURL
 * and revoke on unmount to avoid memory leaks.
 */
export function EvidenceSnapshot({
  paperId,
  page,
  altText,
  variant = 'thumbnail',
}: EvidenceSnapshotProps) {
  const [state, setState] = useState<State>('loading');
  const objectUrlRef = useRef<string | null>(null);
  const [src, setSrc] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    setState('loading');
    setSrc(null);

    fetchSnapshot(paperId, page)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrlRef.current = url;
        setSrc(url);
        setState('ok');
      })
      .catch(() => {
        if (!cancelled) setState('error');
      });

    return () => {
      cancelled = true;
      if (objectUrlRef.current) {
        URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = null;
      }
    };
  }, [paperId, page]);

  const isThumbnail = variant === 'thumbnail';
  const containerClass = isThumbnail
    ? 'w-24 h-32 rounded border border-border overflow-hidden shrink-0'
    : 'w-full rounded border border-border overflow-hidden';

  if (state === 'loading') {
    return <Skeleton className={containerClass} />;
  }

  if (state === 'error' || !src) {
    return (
      <div
        className={`${containerClass} flex items-center justify-center bg-muted text-muted-foreground`}
        aria-label="Snapshot unavailable"
        role="img"
      >
        <ImageOff className="h-5 w-5" />
      </div>
    );
  }

  return (
    <img
      src={src}
      alt={altText ?? `Page ${page} snapshot`}
      className={`${containerClass} object-contain bg-white`}
    />
  );
}
