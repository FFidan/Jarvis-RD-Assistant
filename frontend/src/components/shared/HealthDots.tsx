/**
 * HealthDots — sidebar health-status pill for all stack components.
 *
 * Collapsed (default): single pill summarising overall health.
 *   "All healthy" (green) | "N degraded" (amber) | "N down" (red)
 *
 * Expanded (click): per-service grid showing each component's dot + label.
 *
 * Data is fetched every 30 s via TanStack Query.
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { Link } from 'react-router-dom';
import { cn } from '@/lib/utils';
import { fetchStackHealth, type ServiceHealth, type ServiceHealthStatus } from '@/lib/api';
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function statusColor(status: ServiceHealthStatus): string {
  switch (status) {
    case 'ok':
      return 'bg-green-500';
    case 'degraded':
      return 'bg-amber-400';
    case 'down':
      return 'bg-red-500';
    case 'unknown':
    default:
      return 'bg-gray-400';
  }
}

function pillColor(status: ServiceHealthStatus): string {
  switch (status) {
    case 'ok':
      return 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400';
    case 'degraded':
      return 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400';
    case 'down':
      return 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400';
    case 'unknown':
    default:
      return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400';
  }
}

function pillLabel(overall: ServiceHealthStatus, downCount: number, degradedCount: number): string {
  if (overall === 'ok') return 'All healthy';
  if (overall === 'down') return `${downCount} down`;
  // No probe response within the deadline: every service is 'unknown'.
  if (overall === 'unknown') return 'Status unknown';
  return `${degradedCount} degraded`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Display label for a service — renames "Vector" to the self-hoster-friendly label. */
function serviceDisplayLabel(svc: ServiceHealth): string {
  if (svc.name === 'vector') return 'Log collector (optional)';
  return svc.label;
}

function ServiceRow({ svc }: { svc: ServiceHealth }) {
  const label = serviceDisplayLabel(svc);
  const isVectorUnknown = svc.name === 'vector' && svc.status === 'unknown';
  return (
    <div className="space-y-0.5" data-testid={`health-row-${svc.name}`}>
      <div className="flex items-center gap-2">
        <span
          className={cn('h-2 w-2 shrink-0 rounded-full', statusColor(svc.status))}
          aria-label={`${label}: ${svc.status}`}
        />
        <span className="truncate">{label}</span>
        <span className={cn('ml-auto font-medium capitalize', {
          'text-green-600 dark:text-green-400': svc.status === 'ok',
          'text-amber-600 dark:text-amber-400': svc.status === 'degraded',
          'text-red-600 dark:text-red-400': svc.status === 'down',
          'text-gray-500': svc.status === 'unknown',
        })}>
          {svc.status}
        </span>
      </div>
      {isVectorUnknown && (
        <p className="pl-4 text-[10px] text-muted-foreground leading-tight" data-testid="vector-optional-note">
          Not running — normal unless you enabled observability.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// HealthDots (exported)
// ---------------------------------------------------------------------------

interface HealthDotsProps {
  /** When true the component renders a compact row of dots (sidebar-collapsed mode). */
  compact?: boolean;
  /**
   * When provided (admin users only), clicking the health pill opens a
   * per-service popover with a footer link to this path (full report).
   * Non-admin users keep the in-place expand behavior.
   */
  adminLink?: string;
}

export function HealthDots({ compact = false, adminLink }: HealthDotsProps) {
  const [expanded, setExpanded] = useState(false);

  const { data, isError } = useQuery({
    queryKey: QUERY_KEYS.stack.health(),
    queryFn: fetchStackHealth,
    refetchInterval: 30_000,
    // Don't throw on individual probe failures — fetchStackHealth never rejects
    retry: false,
    placeholderData: (prev) => prev,
  });

  // While loading or if the whole query errored, show a neutral dot
  if (!data || isError) {
    if (compact) {
      return (
        <div className="flex justify-center gap-1" data-testid="health-dots-loading">
          <span className="h-2 w-2 rounded-full bg-gray-400" />
        </div>
      );
    }
    return (
      <div className="space-y-1 text-xs text-muted-foreground" data-testid="health-dots-loading">
        <span className="text-xs text-muted-foreground">Checking services…</span>
      </div>
    );
  }

  const { services, degradedCount, downCount, overall } = data;

  // ----- Compact (sidebar collapsed) mode -----
  if (compact) {
    return (
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex justify-center gap-1 rounded px-1 py-0.5 hover:bg-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={`Stack health: ${pillLabel(overall, downCount, degradedCount)}. Click to expand.`}
        data-testid="health-dots-compact"
      >
        {services.map((svc) => (
          <span
            key={svc.name}
            className={cn('h-2 w-2 rounded-full', statusColor(svc.status))}
            aria-label={`${svc.label}: ${svc.status}`}
          />
        ))}
      </button>
    );
  }

  // ----- Expanded sidebar mode -----

  // Admin users: pill opens a quick per-service popover + footer link to full report.
  if (adminLink) {
    return (
      <div className="space-y-1 text-xs text-muted-foreground" data-testid="health-dots-root">
        <Popover>
          <PopoverTrigger asChild>
            <button
              type="button"
              className={cn(
                'flex w-full items-center gap-2 rounded px-2 py-1 text-xs font-medium transition-colors hover:bg-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                pillColor(overall),
              )}
              aria-label={`Stack health: ${pillLabel(overall, downCount, degradedCount)}. Click to view system health.`}
              data-testid="health-pill-admin-link"
            >
              <span className={cn('h-2 w-2 shrink-0 rounded-full', statusColor(overall))} />
              <span className="flex-1 text-left">{pillLabel(overall, downCount, degradedCount)}</span>
            </button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-64 p-3">
            <div className="space-y-1" data-testid="health-popover-grid">
              {services.map((svc) => (
                <ServiceRow key={svc.name} svc={svc} />
              ))}
            </div>
            <div className="mt-3 border-t border-border pt-2">
              <Link
                to={adminLink}
                className="text-xs text-muted-foreground hover:text-foreground hover:underline"
                data-testid="health-popover-full-report"
              >
                Deployment &amp; service health →
              </Link>
            </div>
          </PopoverContent>
        </Popover>
      </div>
    );
  }

  return (
    <div className="space-y-1 text-xs text-muted-foreground" data-testid="health-dots-root">
      {/* Collapsed pill / toggle row */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          'flex w-full items-center gap-2 rounded px-2 py-1 text-xs font-medium transition-colors hover:bg-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          pillColor(overall),
        )}
        aria-expanded={expanded}
        aria-label={`Stack health: ${pillLabel(overall, downCount, degradedCount)}. Click to ${expanded ? 'collapse' : 'expand'}.`}
        data-testid="health-pill-toggle"
      >
        <span
          className={cn('h-2 w-2 shrink-0 rounded-full', statusColor(overall))}
        />
        <span className="flex-1 text-left">{pillLabel(overall, downCount, degradedCount)}</span>
        {expanded ? (
          <ChevronUp className="h-3 w-3 shrink-0" />
        ) : (
          <ChevronDown className="h-3 w-3 shrink-0" />
        )}
      </button>

      {/* Expanded per-service grid */}
      {expanded && (
        <div
          className="space-y-1 rounded border border-border bg-background p-2"
          data-testid="health-expanded-grid"
        >
          {services.map((svc) => (
            <ServiceRow key={svc.name} svc={svc} />
          ))}
        </div>
      )}
    </div>
  );
}
