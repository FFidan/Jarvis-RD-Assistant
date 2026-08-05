import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { TriageSection } from '@/components/my-day/sections/TriageSection';
import type { MissingFoundationalPaper } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
import { makeFeedPaper } from '@/__tests__/fixtures/feed-paper';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

const { toast } = await import('sonner');

const startJobMock = vi.hoisted(() => vi.fn());

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      jobs: {},
      activeAborts: {},
      hasRunning: () => false,
      isRunning: () => false,
      startJob: startJobMock,
      trackExternalJob: vi.fn(),
    }),
  ),
}));

vi.mock('@/lib/api', () => ({
  fetchFeed: vi.fn(),
  fetchMissingFoundationalPapers: vi.fn(),
  fetchAndProcessFoundationalPaper: vi.fn(),
}));

const { fetchFeed, fetchMissingFoundationalPapers, fetchAndProcessFoundationalPaper } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderSubject() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <TriageSection />
    </MemoryRouter>,
    { queryClient },
  );
}

// ---------------------------------------------------------------------------
// Shared fixture builders
// ---------------------------------------------------------------------------

function makeFoundationalPaper(overrides: Partial<MissingFoundationalPaper> = {}): MissingFoundationalPaper {
  return {
    paper_id: 200,
    title: 'Foundational Paper',
    authors: ['Founder B'],
    year: 2020,
    citation_count: 100,
    cited_by_library_count: 3,
    url: null,
    pdf_available: false,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('TriageSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    startJobMock.mockResolvedValue(undefined);
  });

  it('renders nothing when both action items and foundational papers are empty', async () => {
    vi.mocked(fetchFeed).mockResolvedValue({ papers: [], total: 0 });
    vi.mocked(fetchMissingFoundationalPapers).mockResolvedValue([]);

    const { container } = renderSubject();

    // Wait long enough for queries to resolve
    await new Promise((r) => setTimeout(r, 50));

    // Component returns null when both lists are empty — nothing in the container
    expect(screen.queryByText(/Triage/i)).not.toBeInTheDocument();
    expect(container.firstChild).toBeNull();
  });

  it('renders error sentinel when action items query fails', async () => {
    vi.mocked(fetchFeed).mockRejectedValue(new Error('500'));
    vi.mocked(fetchMissingFoundationalPapers).mockResolvedValue([]);

    renderSubject();

    expect(await screen.findByRole('status')).toHaveTextContent(/unable to load triage/i);
  });

  it('renders error sentinel when foundational papers query fails', async () => {
    vi.mocked(fetchFeed).mockResolvedValue({ papers: [], total: 0 });
    vi.mocked(fetchMissingFoundationalPapers).mockRejectedValue(new Error('500'));

    renderSubject();

    expect(await screen.findByRole('status')).toHaveTextContent(/unable to load triage/i);
  });

  it('renders error sentinel when both queries fail', async () => {
    vi.mocked(fetchFeed).mockRejectedValue(new Error('500'));
    vi.mocked(fetchMissingFoundationalPapers).mockRejectedValue(new Error('500'));

    renderSubject();

    expect(await screen.findByRole('status')).toHaveTextContent(/unable to load triage/i);
  });

  it('renders both action item and foundational paper rows when data is present', async () => {
    vi.mocked(fetchFeed).mockResolvedValue({
      papers: [makeFeedPaper()],
      total: 1,
    });
    vi.mocked(fetchMissingFoundationalPapers).mockResolvedValue([
      makeFoundationalPaper(),
    ]);

    renderSubject();

    // Both titles should appear in the card
    expect(await screen.findByText('Action Item Paper')).toBeInTheDocument();
    expect(screen.getByText('Foundational Paper')).toBeInTheDocument();

    // Section header includes "Triage"
    expect(screen.getByText(/Triage/i)).toBeInTheDocument();
  });

  it('a rejected startJob fires toast.error instead of silently swallowing it', async () => {
    const user = userEvent.setup();
    startJobMock.mockRejectedValue(new Error('queue full'));
    vi.mocked(fetchFeed).mockResolvedValue({
      papers: [makeFeedPaper({ pdf_downloaded: true })],
      total: 1,
    });
    vi.mocked(fetchMissingFoundationalPapers).mockResolvedValue([]);

    renderSubject();

    const processButton = await screen.findByRole('button', { name: 'Process' });
    await user.click(processButton);

    await vi.waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Could not start processing: queue full');
    });
  });

  it('the addMut error path fires toast.error', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchFeed).mockResolvedValue({ papers: [], total: 0 });
    vi.mocked(fetchMissingFoundationalPapers).mockResolvedValue([makeFoundationalPaper()]);
    vi.mocked(fetchAndProcessFoundationalPaper).mockRejectedValue(new Error('locked'));

    renderSubject();

    const addButton = await screen.findByRole('button', { name: /Add & process/ });
    await user.click(addButton);

    await vi.waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Could not add the paper: locked');
    });
  });
});
