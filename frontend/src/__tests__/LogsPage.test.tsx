import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/lib/logs', () => ({
  listEvents: vi.fn().mockResolvedValue({ events: [], next_cursor: null }),
  getSummary: vi.fn().mockResolvedValue({ by_level: {}, by_category: {}, total: 0 }),
  getCorrelation: vi.fn().mockResolvedValue([]),
  getLogsSources: vi.fn().mockResolvedValue([]),
  streamCorrelation: vi.fn().mockReturnValue({ close: vi.fn() }),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    listJobs: vi.fn().mockResolvedValue([]),
    getPulseSourceHealth: vi.fn().mockResolvedValue([]),
    getPulseSourceHistory: vi.fn().mockResolvedValue({}),
  };
});

// ---------------------------------------------------------------------------
// Imports after mocks
// ---------------------------------------------------------------------------

import { LogsPage } from '@/pages/LogsPage';
import { EventsTab } from '@/components/logs/EventsTab';
import { CorrelationGroup } from '@/components/logs/CorrelationGroup';
import { ErrorSparkLine, buildSparkBuckets } from '@/components/logs/ErrorSparkLine';
import { listEvents } from '@/lib/logs';
import type { SystemEvent } from '@/lib/logs';
import { formatTimestamp, formatTime } from '@/lib/relative-time';

const mockListEvents = vi.mocked(listEvents);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeEvent(overrides: Partial<SystemEvent> = {}): SystemEvent {
  return {
    id: Math.floor(Math.random() * 100_000),
    created_at: new Date().toISOString(),
    level: 'info',
    category: 'job',
    source: 'test-source',
    message: 'Default test message',
    context: {},
    correlation_id: null,
    ...overrides,
  };
}

function renderPage(initialPath = '/logs') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <LogsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderEventsTab(initialPath = '/logs?tab=events') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <EventsTab />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// LogsPage — basic navigation (backward-compat with original tests)
// ---------------------------------------------------------------------------

describe('LogsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders page heading', () => {
    renderPage();
    expect(screen.getByText('System Logs')).toBeInTheDocument();
  });

  it('renders all four tab triggers', () => {
    renderPage();
    expect(screen.getByRole('tab', { name: /live/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /jobs/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /sources/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /events/i })).toBeInTheDocument();
  });

  it('defaults to Live tab', () => {
    renderPage();
    const liveTab = screen.getByRole('tab', { name: /live/i });
    expect(liveTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Jobs tab when clicked', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('tab', { name: /jobs/i }));
    expect(screen.getByRole('tab', { name: /jobs/i })).toHaveAttribute('data-state', 'active');
  });

  it('switches to Sources tab when clicked', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('tab', { name: /sources/i }));
    expect(screen.getByRole('tab', { name: /sources/i })).toHaveAttribute('data-state', 'active');
  });

  it('switches to Events tab when clicked', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('tab', { name: /events/i }));
    expect(screen.getByRole('tab', { name: /events/i })).toHaveAttribute('data-state', 'active');
  });

  it('respects ?tab=jobs URL param', () => {
    renderPage('/logs?tab=jobs');
    const jobsTab = screen.getByRole('tab', { name: /jobs/i });
    expect(jobsTab).toHaveAttribute('data-state', 'active');
  });
});

// ---------------------------------------------------------------------------
// Preset selection
// ---------------------------------------------------------------------------

describe('EventsTab — preset selection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListEvents.mockResolvedValue({ events: [], next_cursor: null });
  });

  it('renders the preset dropdown', () => {
    renderEventsTab();
    expect(screen.getByTestId('preset-select')).toBeInTheDocument();
  });

  it('shows all four named presets', () => {
    renderEventsTab();
    const select = screen.getByTestId('preset-select');
    expect(within(select).getByText('Last 1h errors')).toBeInTheDocument();
    expect(within(select).getByText("Today's slow queries")).toBeInTheDocument();
    expect(within(select).getByText('Failed jobs (24h)')).toBeInTheDocument();
    expect(within(select).getByText('Telegram orchestrator runs')).toBeInTheDocument();
  });

  it('selecting "Last 1h errors" triggers an events fetch with level=error', async () => {
    renderEventsTab();
    const select = screen.getByTestId('preset-select');
    await userEvent.selectOptions(select, 'last-1h-errors');
    await waitFor(() => {
      const calls = mockListEvents.mock.calls;
      const lastCall = calls[calls.length - 1]?.[0];
      expect(lastCall).toMatchObject({ level: 'error' });
    });
  });

  it('selecting "Failed jobs (24h)" triggers fetch with category=job and query=failed', async () => {
    renderEventsTab();
    const select = screen.getByTestId('preset-select');
    await userEvent.selectOptions(select, 'failed-jobs-24h');
    await waitFor(() => {
      const calls = mockListEvents.mock.calls;
      const lastCall = calls[calls.length - 1]?.[0];
      expect(lastCall).toMatchObject({ category: 'job', q: 'failed' });
    });
  });

  it('selecting "Telegram orchestrator runs" triggers fetch with query=telegram', async () => {
    renderEventsTab();
    const select = screen.getByTestId('preset-select');
    await userEvent.selectOptions(select, 'telegram-orchestrator');
    await waitFor(() => {
      const calls = mockListEvents.mock.calls;
      const lastCall = calls[calls.length - 1]?.[0];
      expect(lastCall).toMatchObject({ q: 'telegram' });
    });
  });
});

// ---------------------------------------------------------------------------
// Free-text search (client-side filter)
// ---------------------------------------------------------------------------

describe('EventsTab — free-text search', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the search input', () => {
    mockListEvents.mockResolvedValue({ events: [], next_cursor: null });
    renderEventsTab();
    expect(screen.getByTestId('search-input')).toBeInTheDocument();
  });

  it('filters displayed rows by message substring', async () => {
    mockListEvents.mockResolvedValue({
      events: [
        makeEvent({ id: 1, message: 'Alpha event' }),
        makeEvent({ id: 2, message: 'Beta event' }),
        makeEvent({ id: 3, message: 'Alpha again' }),
      ],
      next_cursor: null,
    });
    renderEventsTab();

    await waitFor(() => {
      expect(screen.getByText('Alpha event')).toBeInTheDocument();
      expect(screen.getByText('Beta event')).toBeInTheDocument();
    });

    const input = screen.getByTestId('search-input');
    await userEvent.type(input, 'Alpha');

    expect(screen.getByText('Alpha event')).toBeInTheDocument();
    expect(screen.getByText('Alpha again')).toBeInTheDocument();
    expect(screen.queryByText('Beta event')).not.toBeInTheDocument();
  });

  it('shows all rows when search input is cleared', async () => {
    mockListEvents.mockResolvedValue({
      events: [
        makeEvent({ id: 1, message: 'Alpha event' }),
        makeEvent({ id: 2, message: 'Beta event' }),
      ],
      next_cursor: null,
    });
    renderEventsTab();

    await waitFor(() => screen.getByText('Alpha event'));

    const input = screen.getByTestId('search-input');
    await userEvent.type(input, 'Alpha');
    expect(screen.queryByText('Beta event')).not.toBeInTheDocument();

    await userEvent.clear(input);
    expect(screen.getByText('Alpha event')).toBeInTheDocument();
    expect(screen.getByText('Beta event')).toBeInTheDocument();
  });

  it('is case-insensitive', async () => {
    mockListEvents.mockResolvedValue({
      events: [makeEvent({ id: 1, message: 'UPPERCASE message' })],
      next_cursor: null,
    });
    renderEventsTab();
    await waitFor(() => screen.getByText('UPPERCASE message'));

    const input = screen.getByTestId('search-input');
    await userEvent.type(input, 'uppercase');
    expect(screen.getByText('UPPERCASE message')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Correlation group expand/collapse
// ---------------------------------------------------------------------------

describe('CorrelationGroup — expand/collapse', () => {
  const corrId = 'corr-test-abc123';
  const events: SystemEvent[] = [
    makeEvent({ id: 101, correlation_id: corrId, message: 'First event', level: 'info' }),
    makeEvent({ id: 102, correlation_id: corrId, message: 'Error event', level: 'error' }),
    makeEvent({ id: 103, correlation_id: corrId, message: 'Third event', level: 'info' }),
  ];

  function renderGroup(searchText = '') {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <CorrelationGroup
            correlationId={corrId}
            events={events}
            searchText={searchText}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('renders summary row with event count', () => {
    renderGroup();
    expect(screen.getByText(/3 events/)).toBeInTheDocument();
  });

  it('shows error count badge when errors present', () => {
    renderGroup();
    expect(screen.getByText(/1 error/)).toBeInTheDocument();
  });

  it('event messages are hidden before expand', () => {
    renderGroup();
    expect(screen.queryByText('First event')).not.toBeInTheDocument();
    expect(screen.queryByText('Error event')).not.toBeInTheDocument();
  });

  it('expands on click and shows nested event list', async () => {
    renderGroup();
    const summaryButton = screen.getByTestId(`group-${corrId}`);
    await userEvent.click(summaryButton);
    expect(screen.getByText('First event')).toBeInTheDocument();
    expect(screen.getByText('Error event')).toBeInTheDocument();
    expect(screen.getByText('Third event')).toBeInTheDocument();
  });

  it('collapses on second click', async () => {
    renderGroup();
    const summaryButton = screen.getByTestId(`group-${corrId}`);
    await userEvent.click(summaryButton);
    expect(screen.getByText('First event')).toBeInTheDocument();
    await userEvent.click(summaryButton);
    expect(screen.queryByText('First event')).not.toBeInTheDocument();
  });

  it('searchText filters events within expanded group', async () => {
    renderGroup('Error');
    const summaryButton = screen.getByTestId(`group-${corrId}`);
    await userEvent.click(summaryButton);
    expect(screen.getByText('Error event')).toBeInTheDocument();
    expect(screen.queryByText('First event')).not.toBeInTheDocument();
    expect(screen.queryByText('Third event')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Group-by-correlation toggle
// ---------------------------------------------------------------------------

describe('EventsTab — group by correlation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListEvents.mockResolvedValue({
      events: [
        makeEvent({ id: 1, correlation_id: 'corr-A', message: 'Event A1' }),
        makeEvent({ id: 2, correlation_id: 'corr-A', message: 'Event A2' }),
        makeEvent({ id: 3, correlation_id: 'corr-B', message: 'Event B1' }),
      ],
      next_cursor: null,
    });
  });

  it('renders the group toggle button', () => {
    renderEventsTab();
    expect(screen.getByTestId('group-toggle')).toBeInTheDocument();
  });

  it('in flat mode, all events are visible individually', async () => {
    renderEventsTab();
    await waitFor(() => screen.getByText('Event A1'));
    expect(screen.getByText('Event A2')).toBeInTheDocument();
    expect(screen.getByText('Event B1')).toBeInTheDocument();
  });

  it('in group mode, shows correlation group rows instead of flat events', async () => {
    renderEventsTab();
    await waitFor(() => screen.getByText('Event A1'));

    await userEvent.click(screen.getByTestId('group-toggle'));

    // Groups should be present (summary rows)
    expect(screen.getByTestId('group-corr-A')).toBeInTheDocument();
    expect(screen.getByTestId('group-corr-B')).toBeInTheDocument();

    // Individual messages should be collapsed (not visible in summary)
    expect(screen.queryByText('Event A1')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// ErrorSparkLine — renders; no-error state
// ---------------------------------------------------------------------------

describe('ErrorSparkLine', () => {
  it('shows "no errors" message when no error events exist', () => {
    const events = [makeEvent({ level: 'info' }), makeEvent({ level: 'debug' })];
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <ErrorSparkLine events={events} />
      </QueryClientProvider>,
    );
    expect(screen.getByText(/No errors in the last hour/i)).toBeInTheDocument();
  });

  it('renders spark-line chart when error events exist', () => {
    const events = [makeEvent({ level: 'error', message: 'boom' })];
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <ErrorSparkLine events={events} />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId('error-sparkline')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// LiveTab — Recent Events timestamp includes date
// ---------------------------------------------------------------------------

describe('LiveTab — Recent Events show date in timestamp', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // The recent-events timestamp uses formatTimestamp(), which calls
  // toLocaleString(undefined, …) — i.e. it is LOCALE-sensitive (no fixed
  // 'en-US' pin like formatDate). Asserting a hardcoded "May 8" would be brittle
  // across CI locales. Instead we derive the expected string from the SAME
  // formatter and prove the rendered cell (a) shows that exact string and
  // (b) includes a date portion the time-only formatter does NOT — guarding the
  // regression (bare time string) in any locale.
  const ISO = '2026-05-08T10:00:00Z';

  it('renders the date+time timestamp (not a bare time string) for a recent event row', async () => {
    mockListEvents.mockResolvedValue({
      events: [
        makeEvent({ id: 999, created_at: ISO, message: 'Date display test event' }),
      ],
      next_cursor: null,
    });

    renderPage(); // defaults to Live tab

    await waitFor(() => {
      expect(screen.getByText('Date display test event')).toBeInTheDocument();
    });

    // (a) The cell shows the full date+time string for this ISO.
    const expectedFull = formatTimestamp(ISO);
    expect(screen.getByText(expectedFull)).toBeInTheDocument();

    // (b) Regression guard, locale-independent: the rendered timestamp must
    // include a DATE portion — i.e. it is strictly longer than / not equal to
    // the bare time-only formatting, and contains the day-of-month "8".
    const timeOnly = formatTime(ISO);
    expect(expectedFull).not.toBe(timeOnly);
    expect(expectedFull).toMatch(/8/);
  });

  it('each recent event row timestamp includes both date and time', async () => {
    const iso2 = '2026-05-08T14:30:00Z';
    mockListEvents.mockResolvedValue({
      events: [
        makeEvent({ id: 1001, created_at: ISO, message: 'Event one' }),
        makeEvent({ id: 1002, created_at: iso2, message: 'Event two' }),
      ],
      next_cursor: null,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Event one')).toBeInTheDocument();
      expect(screen.getByText('Event two')).toBeInTheDocument();
    });

    // Each row renders its own date+time timestamp via the locale-aware
    // formatter (asserted against that same formatter, locale-independent).
    expect(screen.getByText(formatTimestamp(ISO))).toBeInTheDocument();
    expect(screen.getByText(formatTimestamp(iso2))).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// buildSparkBuckets unit tests
// ---------------------------------------------------------------------------

describe('buildSparkBuckets', () => {
  it('returns 60 buckets', () => {
    expect(buildSparkBuckets([])).toHaveLength(60);
  });

  it('counts error events in the correct bucket', () => {
    const event: SystemEvent = makeEvent({
      level: 'error',
      // ~30 minutes ago
      created_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
    });
    const buckets = buildSparkBuckets([event]);
    const total = buckets.reduce((s, b) => s + b.errors, 0);
    expect(total).toBe(1);
  });

  it('ignores info events', () => {
    const event: SystemEvent = makeEvent({ level: 'info' });
    const buckets = buildSparkBuckets([event]);
    const total = buckets.reduce((s, b) => s + b.errors, 0);
    expect(total).toBe(0);
  });

  it('counts critical events', () => {
    const event: SystemEvent = makeEvent({
      level: 'critical',
      created_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
    });
    const buckets = buildSparkBuckets([event]);
    const total = buckets.reduce((s, b) => s + b.errors, 0);
    expect(total).toBe(1);
  });

  it('ignores events older than 60 minutes', () => {
    const event: SystemEvent = makeEvent({
      level: 'error',
      created_at: new Date(Date.now() - 90 * 60 * 1000).toISOString(),
    });
    const buckets = buildSparkBuckets([event]);
    const total = buckets.reduce((s, b) => s + b.errors, 0);
    expect(total).toBe(0);
  });
});
