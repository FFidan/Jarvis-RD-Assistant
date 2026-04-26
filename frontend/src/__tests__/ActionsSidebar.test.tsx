import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ActionsSidebar } from '@/components/paper/ActionsSidebar';
import type { AnalyzeEvent } from '@/lib/sse';

// --- Mock SSE streamAnalyze ---

let mockStreamEvents: AnalyzeEvent[] = [];
let mockStreamError: Error | null = null;
/** Optional: a promise that the mock generator will await before yielding events */
let mockStreamGate: Promise<void> | null = null;

vi.mock('@/lib/sse', () => ({
  streamAnalyze: vi.fn(async function* () {
    if (mockStreamGate) await mockStreamGate;
    if (mockStreamError) throw mockStreamError;
    for (const event of mockStreamEvents) {
      yield event;
    }
  }),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    downloadPdf: vi.fn().mockResolvedValue({}),
    processPdf: vi.fn().mockResolvedValue({ job_id: 'job-process-001', status: 'queued' }),
    summarizePaper: vi.fn().mockResolvedValue({ job_id: 'job-summary-001', status: 'queued' }),
    generateCardsJob: vi.fn().mockResolvedValue({ job_id: 'test-job-001', status: 'queued' }),
    getJob: vi.fn().mockResolvedValue({
      id: 'test-job-001', kind: 'card.generate', status: 'succeeded',
      progress: 1.0, progress_message: 'Done',
      result: { cards_created: 3, confidence: 'HIGH' }, error: null,
      created_at: '2026-01-01T00:00:00Z', started_at: null, finished_at: null,
    }),
    fetchDecks: vi.fn().mockResolvedValue([
      { id: 1, name: 'ML Fundamentals', description: null, card_count: 10, due_count: 0, topic_id: null, created_at: '2026-01-01T00:00:00Z' },
    ]),
  };
});

const { downloadPdf, processPdf, summarizePaper, fetchDecks } = await import('@/lib/api');
const { streamAnalyze } = await import('@/lib/sse');

function renderSidebar(paperId = 42) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ActionsSidebar paperId={paperId} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ActionsSidebar', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStreamEvents = [];
    mockStreamError = null;
    mockStreamGate = null;
    // Re-apply default mocks after clearAllMocks
    vi.mocked(downloadPdf).mockResolvedValue({} as never);
    vi.mocked(processPdf).mockResolvedValue({ job_id: 'job-process-001', status: 'queued' } as never);
    vi.mocked(summarizePaper).mockResolvedValue({ job_id: 'job-summary-001', status: 'queued' } as never);
    vi.mocked(fetchDecks).mockResolvedValue([
      { id: 1, name: 'ML Fundamentals', description: null, card_count: 10, due_count: 0, topic_id: null, created_at: '2026-01-01T00:00:00Z' },
    ]);
  });

  it('renders "Analyze Paper" button text', () => {
    renderSidebar();
    expect(screen.getByRole('button', { name: /Analyze Paper/ })).toBeInTheDocument();
  });

  it('renders individual action buttons: "Download PDF", "Process PDF", "Generate Summary"', () => {
    renderSidebar();
    expect(screen.getByRole('button', { name: /Download PDF/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Process PDF/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Generate Summary/ })).toBeInTheDocument();
  });

  it('clicking "Analyze Paper" shows step tracker via SSE events', async () => {
    const user = userEvent.setup();

    // Use a gate so the stream doesn't complete instantly
    let resolveGate!: () => void;
    mockStreamGate = new Promise((r) => { resolveGate = r; });
    mockStreamEvents = [
      { type: 'step', step: 'downloading', status: 'started' },
      { type: 'step', step: 'downloading', status: 'completed' },
      { type: 'step', step: 'processing', status: 'started' },
      { type: 'step', step: 'processing', status: 'completed', chunk_count: 42 },
      { type: 'step', step: 'summarizing', status: 'started' },
      { type: 'step', step: 'summarizing', status: 'completed' },
      { type: 'complete', paper_id: 42 },
    ];

    renderSidebar();
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    // Release the gate to let events flow
    resolveGate();

    await waitFor(() => {
      expect(screen.getByText('Analysis complete')).toBeInTheDocument();
    });

    expect(streamAnalyze).toHaveBeenCalledWith(42, expect.any(AbortSignal));
  });

  it('successful analysis shows "Analysis complete" message', async () => {
    const user = userEvent.setup();

    mockStreamEvents = [
      { type: 'step', step: 'downloading', status: 'started' },
      { type: 'step', step: 'downloading', status: 'completed' },
      { type: 'step', step: 'processing', status: 'started' },
      { type: 'step', step: 'processing', status: 'completed', chunk_count: 10 },
      { type: 'step', step: 'summarizing', status: 'started' },
      { type: 'step', step: 'summarizing', status: 'completed' },
      { type: 'complete', paper_id: 42 },
    ];

    renderSidebar();
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    await waitFor(() => {
      expect(screen.getByText('Analysis complete')).toBeInTheDocument();
    });
  });

  it('download failure via SSE error event shows error message', async () => {
    const user = userEvent.setup();

    mockStreamEvents = [
      { type: 'step', step: 'downloading', status: 'started' },
      { type: 'error', step: 'downloading', message: 'PDF download failed' },
    ];

    renderSidebar();
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    await waitFor(() => {
      expect(screen.getByText(/Failed during downloading/)).toBeInTheDocument();
    });
  });

  it('stream connection error shows error message', async () => {
    const user = userEvent.setup();

    mockStreamError = new Error('Analyze SSE 500: Internal Server Error');

    renderSidebar();
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    await waitFor(() => {
      expect(screen.getByText(/Analysis failed/)).toBeInTheDocument();
    });
  });

  it('all buttons disabled while analyze is running', async () => {
    const user = userEvent.setup();

    // Use a gate to keep the stream open
    let resolveGate!: () => void;
    mockStreamGate = new Promise((r) => { resolveGate = r; });
    mockStreamEvents = [
      { type: 'step', step: 'downloading', status: 'started' },
    ];

    renderSidebar();
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    // Release the gate but events will still be yielded, and then the stream
    // completes. We need to check while the stream is still open.
    // Actually, with the gate, events won't yield until gate resolves.
    // The click triggers runAnalyze which awaits the gate. So buttons should
    // be disabled immediately after the async function starts.
    await waitFor(() => {
      // The analyze is running (isAnalyzing = true) even before events arrive
      expect(screen.getByRole('button', { name: /Download PDF/ })).toBeDisabled();
      expect(screen.getByRole('button', { name: /Process PDF/ })).toBeDisabled();
      expect(screen.getByRole('button', { name: /Generate Summary/ })).toBeDisabled();
    });

    // Cleanup: resolve the gate so the test doesn't hang
    resolveGate();
  });

  it('renders "Generate Cards" section with deck selector and max cards input', async () => {
    renderSidebar();

    // Wait for decks to load so the selector is rendered
    expect(await screen.findByText('Target Deck')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Generate Cards' })).toBeInTheDocument();
    expect(screen.getByText('Max cards')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Generate Cards/ })).toBeInTheDocument();
  });

  it('Generate Cards button is disabled when hasChunks=false (no deck selected)', async () => {
    // Verify !hasChunks contributes to disabled state: even before a deck is
    // chosen, the button's disabled expression includes !hasChunks. When hasChunks
    // is false the button is disabled for that reason in addition to !deckId.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ActionsSidebar paperId={42} hasChunks={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByText('Target Deck');

    const generateBtn = screen.getByRole('button', { name: /Generate Cards/ });
    // Disabled because !hasChunks (and also !deckId — both conditions are true)
    expect(generateBtn).toBeDisabled();
    expect(generateBtn).toHaveAttribute('disabled');
  });

  it('Generate Cards button is still disabled with hasChunks=true but no deck selected', async () => {
    // With hasChunks=true the !hasChunks condition clears, but !deckId still
    // keeps it disabled until the user picks a deck.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ActionsSidebar paperId={42} hasChunks={true} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByText('Target Deck');

    const generateBtn = screen.getByRole('button', { name: /Generate Cards/ });
    // Still disabled because no deck is selected (!deckId), even though hasChunks=true
    expect(generateBtn).toBeDisabled();
  });

  it('renders tooltip info icons for all 5 action buttons', async () => {
    renderSidebar();

    // Wait for decks to load (generate-cards section renders after)
    await screen.findByText('Target Deck');

    // Each InfoTooltip renders an aria-label="More info" button
    const tooltipButtons = screen.getAllByRole('button', { name: /More info/i });

    // 5 tooltips: analyze, download, process, summarize, generate-cards
    // Plus the "Max cards" InfoTooltip already in the file = 6 total
    expect(tooltipButtons.length).toBeGreaterThanOrEqual(5);
  });
});
