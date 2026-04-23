import { AlertTriangle } from 'lucide-react';
import type { SearchPreviewSourceError } from '@/types';
import { SOURCE_LABELS } from '@/components/feed/source-labels';

interface SearchSourceErrorsProps {
  sourceErrors: Record<string, SearchPreviewSourceError>;
}

export function SearchSourceErrors({ sourceErrors }: SearchSourceErrorsProps) {
  const entries = Object.entries(sourceErrors);
  if (entries.length === 0) return null;

  return (
    <div className="space-y-2" aria-live="polite">
      {entries.map(([sourceType, error]) => (
        <div
          key={sourceType}
          className="flex gap-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-100"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="min-w-0 space-y-1">
            <p className="font-medium">
              {SOURCE_LABELS[sourceType] ?? sourceType}
            </p>
            <p>{error.message}</p>
            {(error.status_code !== null || error.retry_after_s !== null) && (
              <p className="text-xs text-amber-800/80 dark:text-amber-200/80">
                {error.status_code !== null && `Status ${error.status_code}`}
                {error.status_code !== null && error.retry_after_s !== null ? ' • ' : ''}
                {error.retry_after_s !== null && `Retry after ${error.retry_after_s}s`}
              </p>
            )}
            {error.settings_hint && (
              <p className="text-xs text-amber-800/80 dark:text-amber-200/80">
                {error.settings_hint}
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
