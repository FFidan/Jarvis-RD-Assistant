import { useState } from 'react';
import { AlertCircle } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from '@/components/ui/sheet';

interface Props {
  ageDays: number;
  diagnostics: Record<string, unknown> | null;
  onRetry?: () => void;
}

function staleBadgeText(ageDays: number): string {
  if (ageDays <= 1) return "Showing yesterday's deck";
  return `Showing deck from ${ageDays} days ago`;
}

interface SourceDiagnostic {
  status?: string;
  message?: string;
  status_code?: number | null;
  retry_after_s?: number | null;
  settings_hint?: string | null;
}

function isDiagnosticRecord(
  value: unknown,
): value is Record<string, SourceDiagnostic> {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value)
  );
}

export function StaleBadge({ ageDays, diagnostics, onRetry }: Props) {
  const [open, setOpen] = useState(false);

  const sourceDiagnostics = isDiagnosticRecord(diagnostics) ? diagnostics : null;

  return (
    <>
      <Badge
        variant="outline"
        className="cursor-pointer gap-1 border-amber-400/60 text-amber-600 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-950/30"
        onClick={() => setOpen(true)}
        role="button"
        aria-label="Stale deck details"
        data-testid="stale-badge"
      >
        <AlertCircle className="h-3 w-3" />
        {staleBadgeText(ageDays)}
      </Badge>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="w-full sm:max-w-md">
          <SheetHeader>
            <SheetTitle>Outdated recommendations</SheetTitle>
            <SheetDescription>
              {staleBadgeText(ageDays)}. A fresh deck hasn&apos;t been generated yet today.
            </SheetDescription>
          </SheetHeader>

          <div className="mt-4 space-y-4">
            {sourceDiagnostics && Object.keys(sourceDiagnostics).length > 0 ? (
              <div className="space-y-2">
                <p className="text-sm font-medium">Source status</p>
                {Object.entries(sourceDiagnostics).map(([source, diag]) => (
                  <div
                    key={source}
                    className="rounded border bg-muted/20 p-2 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{source}</span>
                      {diag.status && (
                        <span
                          className={
                            diag.status === 'ok'
                              ? 'text-[var(--status-ok)]'
                              : diag.status === 'rate_limit'
                                ? 'text-[var(--status-warn)]'
                                : 'text-muted-foreground'
                          }
                        >
                          {diag.status}
                        </span>
                      )}
                    </div>
                    {diag.message && (
                      <p className="mt-1 text-muted-foreground">{diag.message}</p>
                    )}
                    {diag.settings_hint && (
                      <p className="mt-1 text-amber-600 dark:text-amber-400">
                        {diag.settings_hint}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                No source diagnostic information available.
              </p>
            )}

            {onRetry && (
              <Button
                className="w-full"
                onClick={() => {
                  onRetry();
                  setOpen(false);
                }}
              >
                Generate now
              </Button>
            )}
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
