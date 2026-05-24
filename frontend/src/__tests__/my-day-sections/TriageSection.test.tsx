import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TriageSection } from '@/components/my-day/sections/TriageSection';
import type { FeedPaper, MissingFoundationalPaper } from '@/types';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      jobs: {},
      activeAborts: {},
      hasRunning: () => false,
      isRunning: () => false,
      startJob: vi.fn(),
      trackExternalJob: vi.fn(),
    }),
  ),
}));

vi.mock('@/lib/api', () => ({
  fetchFeedPapers: vi.fn(),
  fetchMissingFoundationalPapers: vi.fn(),
  fetchAndProcessFoundationalPaper: vi.fn(),
}));

const { fetchFeedPapers, fetchMissingFoundationalPapers } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TriageSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Shared fixture builders
// ---------------------------------------------------------------------------

function makeFeedPaper(overrides: Partial<FeedPaper> = {}): FeedPaper {
  return {
    id: 1,
    external_id: 'ext-001',
    source_type: 'arxiv' as const,
    title: 'Action Item Paper',
    authors: ['Author A'],
    abstract: null,
    published_date: '2026-01-01',
    url: null,
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    discovered_at: '2026-01-01T00:00:00Z',
    priority_score: null,
    citation_count: null,
    metadata: {},
    created_at: '2026-01-01T00:00:00Z',
    discovery_origin: 'pulse',
    user_state: null,
    recent_feedback: null,
    state: 'inbox' as const,
    state_before_trash: null,
    starred: false,
    rating: null,
    ...overrides,
  };
}

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
  });

  it('renders nothing when both action items and foundational papers are empty', async () => {
    vi.mocked(fetchFeedPapers).mockResolvedValue({ papers: [], total: 0 });
    vi.mocked(fetchMissingFoundationalPapers).mockResolvedValue([]);

    const { container } = renderWithProviders();

    // Wait long enough for queries to resolve
    await new Promise((r) => setTimeout(r, 50));

    // Component returns null when both lists are empty — nothing in the container
    expect(screen.queryByText(/§ Triage/i)).not.toBeInTheDocument();
    expect(container.firstChild).toBeNull();
  });

  it('renders error sentinel when action items query fails', async () => {
    vi.mocked(fetchFeedPapers).mockRejectedValue(new Error('500'));
    vi.mocked(fetchMissingFoundationalPapers).mockResolvedValue([]);

    renderWithProviders();

    expect(await screen.findByRole('status')).toHaveTextContent(/unable to load triage/i);
  });

  it('renders error sentinel when foundational papers query fails', async () => {
    vi.mocked(fetchFeedPapers).mockResolvedValue({ papers: [], total: 0 });
    vi.mocked(fetchMissingFoundationalPapers).mockRejectedValue(new Error('500'));

    renderWithProviders();

    expect(await screen.findByRole('status')).toHaveTextContent(/unable to load triage/i);
  });

  it('renders both action item and foundational paper rows when data is present', async () => {
    vi.mocked(fetchFeedPapers).mockResolvedValue({
      papers: [makeFeedPaper()],
      total: 1,
    });
    vi.mocked(fetchMissingFoundationalPapers).mockResolvedValue([
      makeFoundationalPaper(),
    ]);

    renderWithProviders();

    // Both titles should appear in the card
    expect(await screen.findByText('Action Item Paper')).toBeInTheDocument();
    expect(screen.getByText('Foundational Paper')).toBeInTheDocument();

    // Section header includes "Triage"
    expect(screen.getByText(/Triage/i)).toBeInTheDocument();
  });
});
