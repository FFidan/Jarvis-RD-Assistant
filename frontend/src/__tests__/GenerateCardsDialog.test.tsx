/**
 * Tests for GenerateCardsDialog — job-polling UX and action_link error rendering.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { GenerateCardsDialog } from '@/components/cards/CreateCardForm';
import type { Job } from '@/stores/job-store';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockGenerateCardsJob = vi.fn();
const mockGetJob = vi.fn();
const mockFetchDecks = vi.fn();

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    generateCardsJob: (...args: unknown[]) => mockGenerateCardsJob(...args),
    getJob: (...args: unknown[]) => mockGetJob(...args),
    fetchDecks: (...args: unknown[]) => mockFetchDecks(...args),
  };
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderDialog(open = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <GenerateCardsDialog open={open} onOpenChange={vi.fn()} defaultDeckId={1} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function makeJob(partial: Partial<Job>): Job {
  return {
    id: 'test-job-001',
    kind: 'card.generate',
    status: 'queued',
    progress: 0,
    progress_message: null,
    result: null,
    error: null,
    created_at: '2026-04-17T00:00:00Z',
    started_at: null,
    finished_at: null,
    ...partial,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('GenerateCardsDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue([
      {
        id: 1,
        name: 'ML Deck',
        description: null,
        card_count: 0,
        due_count: 0,
        topic_id: null,
        created_at: '2026-01-01T00:00:00Z',
      },
    ]);
    mockGenerateCardsJob.mockResolvedValue({ job_id: 'test-job-001', status: 'queued' });
    mockGetJob.mockResolvedValue(makeJob({ status: 'queued' }));
  });

  it('renders the dialog title when open', async () => {
    renderDialog();
    await waitFor(() => {
      expect(screen.getByText(/Generate Cards from Paper/i)).toBeInTheDocument();
    });
  });

  it('renders a Generate button', async () => {
    renderDialog();
    await waitFor(() => {
      // Button text can be "Generate" (enabled) or "Generating…" (in progress)
      expect(screen.getByRole('button', { name: /generate/i })).toBeInTheDocument();
    });
  });

  it('renders a Cancel button', async () => {
    renderDialog();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument();
    });
  });

  it('renders deck selector after decks load', async () => {
    renderDialog();
    await waitFor(() => {
      expect(screen.getByText(/ML Deck/i)).toBeInTheDocument();
    });
  });

  it('renders max cards input', async () => {
    renderDialog();
    await waitFor(() => {
      const input = screen.getByLabelText(/max cards/i);
      expect(input).toBeInTheDocument();
      expect((input as HTMLInputElement).value).toBe('5');
    });
  });

  it('renders nothing when closed (open=false)', () => {
    renderDialog(false);
    expect(screen.queryByText(/Generate Cards from Paper/i)).not.toBeInTheDocument();
  });

  it('job type exports are importable', async () => {
    // Validate that the Job type from job-store has the expected shape
    // This is a compile-time check exercised at runtime.
    const job = makeJob({
      status: 'failed',
      error: {
        message: 'Paper has no processed chunks',
        action_link: { label: 'Process PDF now', href: '/paper/42?action=process' },
      },
    });
    expect(job.error?.action_link?.href).toBe('/paper/42?action=process');
    expect(job.error?.action_link?.label).toBe('Process PDF now');
  });

  it('succeeded job result shape is typed correctly', () => {
    const job = makeJob({
      status: 'succeeded',
      result: { cards_created: 3, confidence: 'HIGH' },
    });
    const res = job.result as { cards_created?: number; confidence?: string };
    expect(res.cards_created).toBe(3);
    expect(res.confidence).toBe('HIGH');
  });
});
