/**
 * Named filter presets for the Events tab.
 *
 * Each preset fully specifies the filter state that will be applied when
 * selected from the dropdown. The last-used preset id is persisted in
 * ui-store under `logsPreset`.
 */

export interface LogsPreset {
  id: string;
  label: string;
  level: string;
  category: string;
  source: string;
  /** ISO string or empty. */
  since: string;
  until: string;
  query: string;
}

function hoursAgo(n: number): string {
  return new Date(Date.now() - n * 60 * 60 * 1000).toISOString();
}

function startOfToday(): string {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.toISOString();
}

/**
 * Returns the canonical preset list.
 *
 * Called as a function (rather than a const) so that date values are computed
 * at selection time, not at module-load time.
 */
export function buildPresets(): LogsPreset[] {
  return [
    {
      id: 'last-1h-errors',
      label: 'Last 1h errors',
      level: 'error',
      category: '',
      source: '',
      since: hoursAgo(1),
      until: '',
      query: '',
    },
    {
      id: 'today-slow-queries',
      label: "Today's slow queries",
      level: '',
      category: 'infra',
      source: '',
      since: startOfToday(),
      until: '',
      query: 'slow',
    },
    {
      id: 'failed-jobs-24h',
      label: 'Failed jobs (24h)',
      level: '',
      category: 'job',
      source: '',
      since: hoursAgo(24),
      until: '',
      query: 'failed',
    },
    {
      id: 'telegram-orchestrator',
      label: 'Telegram orchestrator runs',
      level: '',
      category: 'job',
      source: '',
      since: hoursAgo(24),
      until: '',
      query: 'telegram',
    },
  ];
}
