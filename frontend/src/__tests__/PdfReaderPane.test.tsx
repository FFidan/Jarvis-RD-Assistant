import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ScaledPosition } from 'react-pdf-highlighter-extended';
import { PdfReaderPane } from '@/components/paper/PdfReaderPane';
import type { Highlight } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

interface MockSelection {
  position: ScaledPosition;
  content: { text?: string };
}

// Shared, hoist-safe mock state (react-pdf-highlighter-extended + API clients).
const mocks = vi.hoisted<{
  // Current PDF text selection a SelectionTip would read on mount.
  selection: MockSelection | null;
  fetchPdfUrl: Mock;
  listHighlights: Mock;
  createHighlight: Mock;
  updateHighlight: Mock;
  deleteHighlight: Mock;
  zoteroGetLinkage: Mock;
  zoteroPushHighlights: Mock;
  trackExternalJob: Mock;
  isRunning: Mock;
  toastError: Mock;
  // Stand-in for the highlighter's page viewer, handed to the component
  // through the `utilsRef` callback the library normally supplies.
  viewer: { scrollPageIntoView: Mock; getPageView: Mock } | null;
}>(() => ({
  selection: null,
  fetchPdfUrl: vi.fn(),
  listHighlights: vi.fn(),
  createHighlight: vi.fn(),
  updateHighlight: vi.fn(),
  deleteHighlight: vi.fn(),
  zoteroGetLinkage: vi.fn(),
  zoteroPushHighlights: vi.fn(),
  trackExternalJob: vi.fn(),
  isRunning: vi.fn(() => false),
  toastError: vi.fn(),
  viewer: null,
}));

// Lightweight stub of the PDF library so tests exercise OUR adapter / CRUD /
// state wiring rather than pdf.js canvas rendering. Built with createElement to
// stay clear of the vi.mock-factory hoisting pitfalls around JSX imports.
vi.mock('react-pdf-highlighter-extended', async () => {
  const React = await import('react');
  const HLContext = React.createContext<unknown>(null);
  return {
    PdfLoader: ({
      document,
      children,
    }: {
      document: string | null;
      children: (doc: unknown) => React.ReactNode;
    }) => (document ? children({ numPages: 1 }) : null),
    PdfHighlighter: ({
      highlights,
      children,
      selectionTip,
      style,
      utilsRef,
    }: {
      highlights: Array<{ id: string }>;
      children: React.ReactNode;
      selectionTip: React.ReactNode;
      style?: React.CSSProperties;
      utilsRef?: (utils: unknown) => void;
    }) => {
      React.useEffect(() => {
        utilsRef?.({ getViewer: () => mocks.viewer });
      }, [utilsRef]);
      return React.createElement(
        'div',
        { 'data-testid': 'pdf-highlighter', style },
        React.createElement('span', { 'data-testid': 'hl-count' }, String(highlights.length)),
        ...highlights.map((hl) =>
          React.createElement(
            HLContext.Provider,
            { value: { highlight: hl, isScrolledTo: false }, key: hl.id },
            children,
          ),
        ),
        selectionTip,
      );
    },
    TextHighlight: ({
      highlight,
      onClick,
    }: {
      highlight: { id: string; content?: { text?: string } };
      onClick?: () => void;
    }) =>
      React.createElement(
        'button',
        { 'data-testid': 'text-highlight', 'data-id': highlight.id, onClick },
        highlight.content?.text ?? 'highlight',
      ),
    MonitoredHighlightContainer: ({ children }: { children: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useHighlightContainerContext: () => React.useContext(HLContext),
    usePdfHighlighterContext: () => ({
      getCurrentSelection: () => mocks.selection,
      setTip: () => {},
      updateTipPosition: () => {},
    }),
  };
});

// The worker is a runtime-only asset (?url → string). The library is mocked, so
// it is never used; stub it to avoid loading a real asset under jsdom.
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: 'test-worker-url' }));

vi.mock('@/lib/api/papers', () => ({ fetchPdfUrl: (id: number) => mocks.fetchPdfUrl(id) }));
vi.mock('@/lib/api/highlights', () => ({
  listHighlights: (id: number) => mocks.listHighlights(id),
  createHighlight: (id: number, data: unknown) => mocks.createHighlight(id, data),
  updateHighlight: (id: number, data: unknown) => mocks.updateHighlight(id, data),
  deleteHighlight: (id: number) => mocks.deleteHighlight(id),
}));

// Zotero export client + job store, mocked as SEPARATE modules so the button's
// linkage gate, mutation call, and trackExternalJob handoff are independently
// observable.
vi.mock('@/lib/api/zotero', () => ({
  zoteroGetLinkage: (id: number) => mocks.zoteroGetLinkage(id),
  zoteroPushHighlights: (id: number) => mocks.zoteroPushHighlights(id),
}));
vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (s: unknown) => unknown) =>
    selector({ trackExternalJob: mocks.trackExternalJob, isRunning: mocks.isRunning }),
}));

// Shared sonner mock; the error member is additionally routed through the
// hoisted mocks.toastError spy the assertions in this file use.
vi.mock('sonner', async () => {
  const { createSonnerMock } = await import('@/__tests__/fixtures/sonner-mock');
  const mock = createSonnerMock();
  mock.toast.error = vi.fn((...args: unknown[]) => mocks.toastError(...args));
  return mock;
});

function makeHighlight(overrides: Partial<Highlight> = {}): Highlight {
  return {
    id: 5,
    paper_id: 42,
    page: 2,
    rect: {
      boundingRect: { x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.35 },
      rects: [{ x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.35 }],
    },
    note: 'original note',
    color: '#FBBF24',
    quote: 'persisted quote',
    created_at: '2026-06-26T00:00:00Z',
    stale: false,
    ...overrides,
  };
}

function renderPane() {
  const qc = createTestQueryClient();
  return renderWithProviders(
    <PdfReaderPane paperId={42} />,
    { queryClient: qc },
  );
}

beforeEach(() => {
  mocks.selection = null;
  mocks.viewer = null;
  mocks.fetchPdfUrl.mockReset();
  mocks.listHighlights.mockReset();
  mocks.createHighlight.mockReset();
  mocks.updateHighlight.mockReset();
  mocks.deleteHighlight.mockReset();
  mocks.zoteroGetLinkage.mockReset();
  mocks.zoteroPushHighlights.mockReset();
  mocks.trackExternalJob.mockReset();
  mocks.isRunning.mockReset();
  mocks.toastError.mockReset();
  // Defaults: not linked to Zotero, no export job running.
  mocks.zoteroGetLinkage.mockResolvedValue({
    zotero_item_key: null,
    zotero_citation_key: null,
    zotero_last_pushed_at: null,
  });
  mocks.isRunning.mockReturnValue(false);
  // jsdom has no real object-URL registry — keep revoke a harmless no-op.
  URL.revokeObjectURL = vi.fn();
});

describe('PdfReaderPane', () => {
  it('renders persisted highlights from the query', async () => {
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([makeHighlight()]);

    renderPane();

    expect(await screen.findByTestId('hl-count')).toHaveTextContent('1');
    expect(screen.getByTestId('text-highlight')).toHaveTextContent('persisted quote');
  });

  it('creates a highlight from a text selection and shows it after refetch', async () => {
    const created = makeHighlight({ id: 9, note: 'my note', quote: 'selected quote' });
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValueOnce([]).mockResolvedValue([created]);
    mocks.createHighlight.mockResolvedValue(created);
    // A library ScaledPosition for a 612x792 page → normalizes to x0 0.1, y0 0.2.
    mocks.selection = {
      position: {
        boundingRect: { x1: 61.2, y1: 158.4, x2: 306, y2: 277.2, width: 612, height: 792, pageNumber: 2 },
        rects: [{ x1: 61.2, y1: 158.4, x2: 306, y2: 277.2, width: 612, height: 792, pageNumber: 2 }],
        usePdfCoordinates: false,
      },
      content: { text: 'selected quote' },
    };

    renderPane();

    const noteInput = await screen.findByLabelText('Highlight note');
    await userEvent.type(noteInput, 'my note');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(mocks.createHighlight).toHaveBeenCalledWith(
      42,
      expect.objectContaining({
        page: 2,
        note: 'my note',
        quote: 'selected quote',
        color: '#FBBF24',
        rect: expect.objectContaining({
          boundingRect: expect.objectContaining({ x0: expect.closeTo(0.1, 5), y0: expect.closeTo(0.2, 5) }),
        }),
      }),
    );
    // Invalidation refetches → the new highlight is rendered.
    expect(await screen.findByText('selected quote')).toBeInTheDocument();
  });

  it('edits an existing highlight note and persists via updateHighlight', async () => {
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([makeHighlight()]);
    mocks.updateHighlight.mockResolvedValue(makeHighlight({ note: 'updated note' }));

    renderPane();

    await userEvent.click(await screen.findByTestId('text-highlight'));
    const editor = screen.getByTestId('highlight-editor');
    const noteInput = within(editor).getByLabelText('Edit highlight note');
    await userEvent.clear(noteInput);
    await userEvent.type(noteInput, 'updated note');
    await userEvent.click(within(editor).getByRole('button', { name: 'Save' }));

    expect(mocks.updateHighlight).toHaveBeenCalledWith(5, expect.objectContaining({ note: 'updated note' }));
  });

  it('confirms before deleting and calls deleteHighlight', async () => {
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([makeHighlight()]);
    mocks.deleteHighlight.mockResolvedValue(undefined);

    renderPane();

    await userEvent.click(await screen.findByTestId('text-highlight'));
    const editor = screen.getByTestId('highlight-editor');
    await userEvent.click(within(editor).getByRole('button', { name: /delete/i }));

    const dialog = await screen.findByRole('alertdialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(mocks.deleteHighlight).toHaveBeenCalledWith(5);
  });

  it('shows an honest degraded panel when the PDF cannot be loaded', async () => {
    mocks.fetchPdfUrl.mockRejectedValue(new Error('PDF not found'));
    mocks.listHighlights.mockResolvedValue([]);

    renderPane();

    expect(await screen.findByTestId('pdf-reader-degraded')).toBeInTheDocument();
    expect(screen.getByText(/could not be loaded/i)).toBeInTheDocument();
    expect(screen.getByText(/PDF not found/)).toBeInTheDocument();
  });

  it('renders the PDF area with no highlights without crashing', async () => {
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([]);

    renderPane();

    const highlighter = await screen.findByTestId('pdf-highlighter');
    const surface = screen.getByTestId('pdf-reader-surface');
    expect(surface).toHaveClass('relative', 'h-[70vh]');
    expect(highlighter).not.toHaveStyle({ position: 'relative' });
    expect(screen.getByTestId('hl-count')).toHaveTextContent('0');
    expect(screen.queryByTestId('text-highlight')).not.toBeInTheDocument();
  });
});

describe('PdfReaderPane — export highlights to Zotero', () => {
  const syncButton = () =>
    screen.getByRole('button', { name: /sync highlights to zotero/i });

  it('disables the export button when the paper is not linked to Zotero', async () => {
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([]);
    // Default linkage in beforeEach = not linked.

    renderPane();

    await screen.findByTestId('pdf-highlighter');
    expect(syncButton()).toBeDisabled();
    expect(mocks.zoteroPushHighlights).not.toHaveBeenCalled();
  });

  it('enables the button when linked and enqueues a push-highlights job on click', async () => {
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([]);
    mocks.zoteroGetLinkage.mockResolvedValue({
      zotero_item_key: 'ITEM1234',
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    });
    mocks.zoteroPushHighlights.mockResolvedValue({ job_id: 'job-123', status: 'queued' });

    renderPane();

    await waitFor(() => expect(syncButton()).toBeEnabled());
    await userEvent.click(syncButton());

    expect(mocks.zoteroPushHighlights).toHaveBeenCalledWith(42);
    await waitFor(() =>
      expect(mocks.trackExternalJob).toHaveBeenCalledWith({
        jobId: 'job-123',
        kind: 'zotero.push_highlights',
        payload: { paper_id: 42 },
        status: 'queued',
      }),
    );
  });

  it('toasts when the export request fails', async () => {
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([]);
    mocks.zoteroGetLinkage.mockResolvedValue({
      zotero_item_key: 'ITEM1234',
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    });
    mocks.zoteroPushHighlights.mockRejectedValue(new Error('boom'));

    renderPane();

    await waitFor(() => expect(syncButton()).toBeEnabled());
    await userEvent.click(syncButton());

    await waitFor(() =>
      expect(mocks.toastError).toHaveBeenCalledWith(
        'Failed to export highlights to Zotero',
        expect.objectContaining({ description: expect.any(String) }),
      ),
    );
    expect(mocks.trackExternalJob).not.toHaveBeenCalled();
  });

  it('shows a busy state while an export job is already running', async () => {
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([]);
    mocks.zoteroGetLinkage.mockResolvedValue({
      zotero_item_key: 'ITEM1234',
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
    });
    mocks.isRunning.mockReturnValue(true); // a push-highlights job is non-terminal

    renderPane();

    const button = await screen.findByRole('button', { name: /exporting/i });
    expect(button).toBeDisabled();
  });
});

describe('PdfReaderPane — evidence anchor jump and quote flash', () => {
  // The text layer is one span per LINE, and extraction hyphenates across
  // them, so this page is written the way a real one breaks: the quote starts
  // mid-line, crosses three spans, and is split by a hyphen.
  const LINES = [
    'The Transformer achieves 28.4 BLEU on the WMT',
    '2014 English-to-German translation task, im-',
    'proving over the best previously reported results.',
  ];
  const QUOTE = 'on the WMT 2014 English-to-German translation task, improving over';

  function buildPageView(): { div: HTMLElement; spans: HTMLElement[] } {
    const div = document.createElement('div');
    const textLayer = document.createElement('div');
    textLayer.className = 'textLayer';
    const spans = LINES.map((line) => {
      const span = document.createElement('span');
      span.textContent = line;
      span.scrollIntoView = vi.fn();
      textLayer.appendChild(span);
      return span;
    });
    div.appendChild(textLayer);
    document.body.appendChild(div);
    return { div, spans };
  }

  it('opens the requested page and flashes every span the quote overlaps', async () => {
    const { div, spans } = buildPageView();
    mocks.viewer = {
      scrollPageIntoView: vi.fn(),
      getPageView: vi.fn(() => ({ div })),
    };
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([]);

    renderPane();
    await screen.findByTestId('pdf-highlighter');

    window.dispatchEvent(
      new CustomEvent('jarvis:pdf-goto', { detail: { page: 3, quote: QUOTE } }),
    );

    expect(mocks.viewer.scrollPageIntoView).toHaveBeenCalledWith({ pageNumber: 3 });
    // Page numbers are 1-based; the viewer's page views are 0-based.
    expect(mocks.viewer.getPageView).not.toHaveBeenCalledWith(3);

    await waitFor(
      () => expect(spans[0]!.style.backgroundColor).not.toBe(''),
      { timeout: 3000 },
    );
    expect(spans[1]!.style.backgroundColor).not.toBe('');
    expect(spans[2]!.style.backgroundColor).not.toBe('');
    expect(spans[0]!.scrollIntoView).toHaveBeenCalled();

    div.remove();
  });

  it('leaves the page alone when the quote is too short to identify', async () => {
    const { div, spans } = buildPageView();
    mocks.viewer = {
      scrollPageIntoView: vi.fn(),
      getPageView: vi.fn(() => ({ div })),
    };
    mocks.fetchPdfUrl.mockResolvedValue('blob:fake');
    mocks.listHighlights.mockResolvedValue([]);

    renderPane();
    await screen.findByTestId('pdf-highlighter');

    window.dispatchEvent(new CustomEvent('jarvis:pdf-goto', { detail: { page: 3, quote: 'the' } }));

    expect(mocks.viewer.scrollPageIntoView).toHaveBeenCalledWith({ pageNumber: 3 });
    await new Promise((resolve) => setTimeout(resolve, 900));
    expect(spans.every((span) => span.style.backgroundColor === '')).toBe(true);

    div.remove();
  });
});
