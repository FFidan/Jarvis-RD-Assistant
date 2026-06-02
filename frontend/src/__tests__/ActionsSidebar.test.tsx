import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { create } from 'zustand';
import { ActionsSidebar } from '@/components/paper/ActionsSidebar';
import type { AnalyzeEvent } from '@/lib/sse';
import type { Job } from '@/stores/job-store';

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

// --- Mock job-store ---
//
// Backed by a REAL (minimal) zustand store so that placing a terminal Job into
// `jobs` triggers a component re-render — exactly how the production store
// drives the ActionsSidebar terminal-feedback useEffect. trackExternalJob and
// isRunning remain spies so call assertions still work.

interface MockJobState {
  trackExternalJob: (job: { jobId: string }) => string;
  isRunning: () => boolean;
  jobs: Record<string, Job>;
}

const jobStoreMocks = vi.hoisted(() => ({
  trackExternalJob: vi.fn((job: { jobId: string }) => job.jobId),
  isRunning: vi.fn(() => false),
}));

const useMockJobStore = create<MockJobState>(() => ({
  trackExternalJob: jobStoreMocks.trackExternalJob,
  isRunning: jobStoreMocks.isRunning,
  jobs: {},
}));

/** Place a terminal (or any) Job into the mock store, triggering a re-render. */
function setMockJob(jobId: string, job: Job) {
  act(() => {
    useMockJobStore.setState((s) => ({ jobs: { ...s.jobs, [jobId]: job } }));
  });
}

function makeJob(overrides: Partial<Job> & Pick<Job, 'id' | 'status'>): Job {
  return {
    kind: 'card.generate',
    progress: 1,
    progress_message: null,
    payload: { paper_id: 42, deck_id: 1 },
    result: null,
    error: null,
    created_at: '2026-01-01T00:00:00Z',
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

vi.mock('@/stores/job-store', () => ({
  useJobStore: <T,>(selector: (state: MockJobState) => T): T =>
    useMockJobStore(selector),
}));

// --- Mock handleAuthFailure from @/lib/api/core ---

const apiCoreMocks = vi.hoisted(() => ({
  handleAuthFailure: vi.fn(),
  apiFetch: vi.fn(),
}));
vi.mock('@/lib/api/core', () => ({
  handleAuthFailure: apiCoreMocks.handleAuthFailure,
  apiFetch: apiCoreMocks.apiFetch,
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    downloadPdf: vi.fn().mockResolvedValue({}),
    processPdf: vi.fn().mockResolvedValue({ job_id: 'job-process-001', status: 'queued' }),
    summarizePaper: vi.fn().mockResolvedValue({ job_id: 'job-summary-001', status: 'queued' }),
    generateCardsJob: vi.fn().mockResolvedValue({ job_id: 'test-job-001', status: 'queued' }),
    fetchDecks: vi.fn().mockResolvedValue([
      { id: 1, name: 'ML Fundamentals', description: null, card_count: 10, due_count: 0, topic_id: null, created_at: '2026-01-01T00:00:00Z' },
    ]),
  };
});

const { downloadPdf, processPdf, summarizePaper, fetchDecks } = await import('@/lib/api');
const { streamAnalyze } = await import('@/lib/sse');

// Radix UI Select uses pointer-capture and scrollIntoView APIs not present in jsdom.
// Polyfill them globally so Select interaction tests can open the dropdown and pick options.
beforeAll(() => {
  if (!window.HTMLElement.prototype.hasPointerCapture) {
    window.HTMLElement.prototype.hasPointerCapture = () => false;
  }
  if (!window.HTMLElement.prototype.setPointerCapture) {
    window.HTMLElement.prototype.setPointerCapture = () => {};
  }
  if (!window.HTMLElement.prototype.releasePointerCapture) {
    window.HTMLElement.prototype.releasePointerCapture = () => {};
  }
  if (!window.HTMLElement.prototype.scrollIntoView) {
    window.HTMLElement.prototype.scrollIntoView = () => {};
  }
});

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
    // Re-apply job-store mock defaults
    jobStoreMocks.trackExternalJob.mockImplementation((job: { jobId: string }) => job.jobId);
    jobStoreMocks.isRunning.mockReturnValue(false);
    useMockJobStore.setState({
      trackExternalJob: jobStoreMocks.trackExternalJob,
      isRunning: jobStoreMocks.isRunning,
      jobs: {},
    });
  });

  it('renders "Analyze Paper" button text', () => {
    renderSidebar();
    expect(screen.getByRole('button', { name: /Analyze Paper/ })).toBeInTheDocument();
  });

  it('renders manual action buttons conditionally based on stage props', async () => {
    const user = userEvent.setup();

    // Default props (pdfDownloaded=false): only "Download PDF" visible behind "Show advanced"
    const { unmount } = renderSidebar();
    await user.click(screen.getByRole('button', { name: /Show advanced/ }));
    expect(screen.getByRole('button', { name: /Download PDF/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Process PDF/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Generate Summary/ })).not.toBeInTheDocument();
    unmount();

    // pdfDownloaded=true, hasChunks=false: only "Process PDF" visible
    const queryClient2 = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount: u2 } = render(
      <QueryClientProvider client={queryClient2}>
        <MemoryRouter>
          <ActionsSidebar paperId={42} pdfDownloaded={true} hasChunks={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole('button', { name: /Show advanced/ }));
    expect(screen.queryByRole('button', { name: /Download PDF/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Process PDF/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Generate Summary/ })).not.toBeInTheDocument();
    u2();

    // hasChunks=true, hasSummary=false: only "Generate Summary" visible
    const queryClient3 = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient3}>
        <MemoryRouter>
          <ActionsSidebar paperId={42} pdfDownloaded={true} hasChunks={true} hasSummary={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole('button', { name: /Show advanced/ }));
    expect(screen.queryByRole('button', { name: /Download PDF/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Process PDF/ })).not.toBeInTheDocument();
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

  it('structured process error shows error_type:error_detail and Retry button', async () => {
    const user = userEvent.setup();

    mockStreamEvents = [
      { type: 'step', step: 'downloading', status: 'started' },
      { type: 'step', step: 'downloading', status: 'completed' },
      { type: 'step', step: 'processing', status: 'started' },
      // Backend emits BOTH old (step+message) AND new (stage+error_type+error_detail)
      // shapes so the existing step-tracker keeps working AND the new structured banner +
      // per-stage Retry can render.
      {
        type: 'error',
        step: 'processing',
        message: 'PDF processing failed',
        error_type: 'PdfReadError',
        error_detail: 'Stream end was reached early',
      } as never,
    ];

    renderSidebar();
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    // Banner shows the structured "error_type: error_detail" string, not the generic
    // "Failed during processing: …" fallback.
    await waitFor(() => {
      expect(
        screen.getByText(/PdfReadError: Stream end was reached early/),
      ).toBeInTheDocument();
    });

    // Per-stage Retry button is offered for the failed stage (processing).
    expect(screen.getByRole('button', { name: /Retry processing/i })).toBeInTheDocument();
  });

  it('safe analyze error display prefers display_message and error_code over raw internals', async () => {
    const user = userEvent.setup();

    mockStreamEvents = [
      { type: 'step', step: 'downloading', status: 'completed' },
      { type: 'step', step: 'processing', status: 'started' },
      {
        type: 'error',
        step: 'processing',
        message: 'PDF processing failed',
        error_type: 'KeyError',
        error_detail: 'encoder registry missing key',
        error_code: 'PDF_PROCESSING_FAILED',
        display_message: 'The PDF could not be processed. Try another source.',
      } as never,
    ];

    renderSidebar();
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    await waitFor(() => {
      expect(screen.getByText(/PDF_PROCESSING_FAILED/)).toBeInTheDocument();
      expect(
        screen.getByText(/The PDF could not be processed\. Try another source\./),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/KeyError/)).not.toBeInTheDocument();
    expect(screen.queryByText(/encoder registry/)).not.toBeInTheDocument();
  });

  it('safe analyze error display uses error_code without falling back to raw internals', async () => {
    const user = userEvent.setup();

    mockStreamEvents = [
      {
        type: 'error',
        step: 'processing',
        message: 'PDF processing failed',
        error_type: 'KeyError',
        error_detail: 'encoder registry missing key',
        error_code: 'PDF_PROCESSING_FAILED',
      } as never,
    ];

    renderSidebar();
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    await waitFor(() => {
      expect(screen.getByText('PDF_PROCESSING_FAILED')).toBeInTheDocument();
    });
    expect(screen.queryByText(/KeyError/)).not.toBeInTheDocument();
    expect(screen.queryByText(/encoder registry/)).not.toBeInTheDocument();
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

  it('visible manual button is disabled while analyze is running', async () => {
    const user = userEvent.setup();

    // Use a gate to keep the stream open
    let resolveGate!: () => void;
    mockStreamGate = new Promise((r) => { resolveGate = r; });
    mockStreamEvents = [
      { type: 'step', step: 'downloading', status: 'started' },
    ];

    renderSidebar();
    // Expand the collapsible BEFORE clicking Analyze (manual buttons are hidden by default)
    await user.click(screen.getByRole('button', { name: /Show advanced/ }));
    await user.click(screen.getByRole('button', { name: /Analyze Paper/ }));

    await waitFor(() => {
      // Default props (pdfDownloaded=false) → only Download PDF is rendered
      expect(screen.getByRole('button', { name: /Download PDF/ })).toBeDisabled();
    });

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

  // ----- card.generate job tracked via job-store (replaced setInterval polling) -----
  //
  // PR3-T6: generateCardsJob result is now wired into the job-store via
  // trackExternalJob, not a manual setInterval(getJob, 1000) loop. This
  // ensures 401s during SSE tracking route through handleAuthFailure (the
  // store's subscriber calls handleAuthFailure on 401 and stops — no infinite
  // reconnect).

  it('Generate Cards wires the job into the store via trackExternalJob, not setInterval polling', async () => {
    const user = userEvent.setup();

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ActionsSidebar paperId={42} hasChunks pdfDownloaded hasSummary />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // Wait for decks to load, select the deck, then click Generate Cards
    await screen.findByText('Target Deck');
    await user.click(screen.getByRole('combobox'));
    await user.click(await screen.findByRole('option', { name: 'ML Fundamentals' }));
    await user.click(screen.getByRole('button', { name: /Generate Cards/ }));

    await waitFor(() => {
      expect(jobStoreMocks.trackExternalJob).toHaveBeenCalledWith({
        jobId: 'test-job-001',
        kind: 'card.generate',
        payload: { paper_id: 42, deck_id: 1 },
        status: 'queued',
      });
    });
  });

  it('Generate Cards delegates tracking to the store exactly once (no setInterval poll loop)', async () => {
    const user = userEvent.setup();

    // The component's only responsibility is to hand the job off to the store
    // via trackExternalJob — once. It does NOT run its own setInterval(getJob)
    // poll loop (the source of the prior 401 leak). 401→handleAuthFailure
    // routing lives in the STORE's SSE subscriber and is covered by the
    // job-store's own tests; asserting it here would be circular.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ActionsSidebar paperId={42} hasChunks pdfDownloaded hasSummary />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByText('Target Deck');
    await user.click(screen.getByRole('combobox'));
    await user.click(await screen.findByRole('option', { name: 'ML Fundamentals' }));
    await user.click(screen.getByRole('button', { name: /Generate Cards/ }));

    await waitFor(() => {
      expect(jobStoreMocks.trackExternalJob).toHaveBeenCalledTimes(1);
    });

    // Give any (hypothetical) poll loop a chance to fire repeated calls — it must not.
    await new Promise((r) => setTimeout(r, 50));
    expect(jobStoreMocks.trackExternalJob).toHaveBeenCalledTimes(1);
  });

  // ----- card.generate TERMINAL feedback banner (restored after PR3 regression) -----
  //
  // The store only invalidates queries (success) / fires a transient toast
  // (failure). The PERSISTENT, actionable banner is the component's job: a
  // useEffect watches the tracked genJob's status and sets actionResult on
  // terminal. These tests drive a terminal Job into the mock store to fire it.

  /** Render with all stages done, select the deck, and click Generate Cards. */
  async function startGenerate() {
    const user = userEvent.setup();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ActionsSidebar paperId={42} hasChunks pdfDownloaded hasSummary />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText('Target Deck');
    await user.click(screen.getByRole('combobox'));
    await user.click(await screen.findByRole('option', { name: 'ML Fundamentals' }));
    await user.click(screen.getByRole('button', { name: /Generate Cards/ }));
    await waitFor(() => expect(jobStoreMocks.trackExternalJob).toHaveBeenCalled());
  }

  it('a succeeded card.generate job shows the "Generated N cards" success banner', async () => {
    await startGenerate();

    // Store transitions the tracked job (id 'test-job-001') to succeeded with
    // the backend result shape ({cards_created, confidence}).
    setMockJob('test-job-001', makeJob({
      id: 'test-job-001',
      status: 'succeeded',
      result: { cards_created: 7, confidence: 'HIGH' },
    }));

    await waitFor(() => {
      expect(screen.getByText('Generated 7 cards (confidence: HIGH)')).toBeInTheDocument();
    });
  });

  it('a failed card.generate job with a SAFE action_link renders a recovery Link', async () => {
    await startGenerate();

    setMockJob('test-job-001', makeJob({
      id: 'test-job-001',
      status: 'failed',
      error: {
        message: 'Paper not processed yet',
        action_link: { label: 'Process Paper first', href: '/paper/1?action=process' },
      },
    }));

    await waitFor(() => {
      expect(screen.getByText('Paper not processed yet')).toBeInTheDocument();
    });
    // Safe relative href → rendered as a navigable Link (role=link).
    const link = screen.getByRole('link', { name: 'Process Paper first' });
    expect(link).toHaveAttribute('href', '/paper/1?action=process');
  });

  it('a failed card.generate job with an UNSAFE action_link renders an inert span, not a Link', async () => {
    await startGenerate();

    setMockJob('test-job-001', makeJob({
      id: 'test-job-001',
      status: 'failed',
      error: {
        message: 'Generation failed',
        action_link: { label: 'Click here', href: 'javascript:alert(1)' },
      },
    }));

    await waitFor(() => {
      expect(screen.getByText('Generation failed')).toBeInTheDocument();
    });
    // Unsafe href → label shown as inert text, NOT a Link.
    expect(screen.getByText('Click here')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Click here' })).not.toBeInTheDocument();
  });

  it('renders tooltip info icons for visible action buttons', async () => {
    const user = userEvent.setup();

    // Render with all stages incomplete-but-progressed enough that manual
    // buttons could appear when expanded. With default props only Download is
    // visible; we expand to surface its InfoTooltip too.
    renderSidebar();
    await screen.findByText('Target Deck');
    await user.click(screen.getByRole('button', { name: /Show advanced/ }));

    // Each InfoTooltip renders an aria-label="More info" button
    const tooltipButtons = screen.getAllByRole('button', { name: /More info/i });

    // Always-visible: analyze + generate-cards + Max cards = 3.
    // After "Show advanced": adds the visible manual-step tooltip(s).
    // With default props (pdfDownloaded=false), only "Download PDF" manual button is shown → +1 = 4.
    expect(tooltipButtons.length).toBeGreaterThanOrEqual(4);
  });
});
