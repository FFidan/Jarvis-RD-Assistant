/**
 * OfflineIndicator — small per-surface badge for the offline / PWA track
 * (Wave 3 P1d).
 *
 * Contract reference:
 *   internal design spec (archived)
 *   "Offline / PWA contract — CANONICAL" §5 (per-view offline-state indicator
 *   system) + per-surface table.
 *
 * Three variants:
 *   - `available-offline`  — Library list, paper reading column, notes (read-only).
 *   - `stale-cached`       — Same surfaces when timestamp is known (shows "as of T").
 *   - `online-only`        — Inbox/Discovery/Search, pipeline actions, RAG, Zotero.
 *
 * Online surfaces: the indicator is NOT rendered. ONLINE rendering unchanged.
 *
 * Usage:
 *   <OfflineIndicator variant="available-offline" />
 *   <OfflineIndicator variant="stale-cached" timestamp={ts} />
 *   <OfflineIndicator variant="online-only" label="Search" />
 */
import { WifiOff, CloudOff, CheckCircle } from 'lucide-react';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Timestamp formatting
// ---------------------------------------------------------------------------

/** Format epoch-ms into a compact human-readable "as of" string. */
function formatCacheTime(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60_000);
  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays}d ago`;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type OfflineIndicatorVariant =
  | 'available-offline'
  | 'stale-cached'
  | 'online-only';

export interface OfflineIndicatorProps {
  variant: OfflineIndicatorVariant;
  /** Epoch-ms of last cache write — required for `stale-cached`. */
  timestamp?: number | null;
  /** Optional label for `online-only` (e.g., "Search"). */
  label?: string;
  className?: string;
}

// ---------------------------------------------------------------------------
// OfflineIndicator
// ---------------------------------------------------------------------------

export function OfflineIndicator({
  variant,
  timestamp,
  label,
  className,
}: OfflineIndicatorProps) {
  if (variant === 'available-offline') {
    return (
      <span
        data-testid="offline-indicator-available"
        className={cn(
          'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
          'bg-emerald-50 text-emerald-700 border border-emerald-200',
          'dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800',
          className,
        )}
      >
        <CheckCircle className="h-3 w-3" aria-hidden="true" />
        available offline
      </span>
    );
  }

  if (variant === 'stale-cached') {
    const timeStr =
      timestamp != null ? formatCacheTime(timestamp) : 'unknown time';
    return (
      <span
        data-testid="offline-indicator-stale"
        role="status"
        title={timestamp != null ? new Date(timestamp).toLocaleString() : undefined}
        className={cn(
          'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
          'bg-amber-50 text-amber-700 border border-amber-200',
          'dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800',
          className,
        )}
      >
        <CloudOff className="h-3 w-3" aria-hidden="true" />
        stale-cached &middot; as of {timeStr}
      </span>
    );
  }

  // online-only
  return (
    <span
      data-testid="offline-indicator-online-only"
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
        'bg-muted text-muted-foreground border border-hair',
        className,
      )}
    >
      <WifiOff className="h-3 w-3" aria-hidden="true" />
      {label ? `${label} · ` : ''}online-only
    </span>
  );
}
