import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { EvidenceTab } from '@/components/paper/EvidenceTab';
import { ChunksTab, passageAnchorId } from '@/components/paper/ChunksTab';
import { PDF_GOTO_EVENT } from '@/lib/pdf-events';
import type { Chunk, KeyFinding, Summary } from '@/types';

function keyFinding(overrides: Partial<KeyFinding> = {}): KeyFinding {
  return {
    finding: 'A verified finding.',
    quote: 'A supporting quote.',
    page_number: 7,
    chunk_id: 12,
    verified: true,
    snapshot_path: null,
    ...overrides,
  };
}

function summary(findings: KeyFinding[]): Summary {
  return {
    id: 1,
    paper_id: 42,
    summary_brief: 'brief',
    summary_detailed: 'detailed',
    tldr: null,
    key_findings: findings,
    methodology: null,
    limitations: null,
    relevance_notes: null,
    confidence: 'HIGH',
    cross_references: [],
    llm_model: null,
    summary_verified: true,
    created_at: '2026-06-23T00:00:00Z',
  };
}

function renderTab(s: Summary, paperId?: number, pdfAvailable = true) {
  return render(
    <MemoryRouter>
      <EvidenceTab summary={s} paperId={paperId} pdfAvailable={pdfAvailable} />
    </MemoryRouter>,
  );
}

function chunk(overrides: Partial<Chunk> = {}): Chunk {
  return {
    id: 12,
    paper_id: 42,
    chunk_index: 1,
    content: 'A supporting quote in its full passage.',
    page_number: 7,
    start_char: 0,
    end_char: 39,
    embedding_id: null,
    created_at: '2026-06-23T00:00:00Z',
    ...overrides,
  };
}

function LazyPassages({ chunks }: { chunks: Chunk[] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <section id="section-chunks">
      <button
        type="button"
        data-testid="chunks-expand-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded(true)}
      >
        Show passages
      </button>
      {expanded && <ChunksTab chunks={chunks} />}
    </section>
  );
}

describe('EvidenceTab', () => {
  it('renders no self-link back to the paper the reader is already on', () => {
    renderTab(summary([keyFinding()]), 42);

    expect(screen.queryByRole('link', { name: /open paper/i })).not.toBeInTheDocument();
    expect(screen.getByText('Verified Findings')).toBeInTheDocument();
  });
});

describe('EvidenceTab — evidence anchors', () => {
  let sectionPdf: HTMLElement;
  let sectionChunks: HTMLElement;

  beforeEach(() => {
    // Per-element stubs, so an assertion proves WHICH section was scrolled to.
    sectionPdf = document.createElement('div');
    sectionPdf.id = 'section-pdf';
    sectionPdf.scrollIntoView = vi.fn();
    sectionChunks = document.createElement('div');
    sectionChunks.id = 'section-chunks';
    sectionChunks.scrollIntoView = vi.fn();
    document.body.append(sectionPdf, sectionChunks);
  });

  afterEach(() => {
    sectionPdf.remove();
    sectionChunks.remove();
  });

  it('page anchor scrolls to the reader and asks it for that page and quote', async () => {
    const goto = vi.fn();
    window.addEventListener(PDF_GOTO_EVENT, goto);
    renderTab(summary([keyFinding()]), 42);

    await userEvent.click(screen.getByRole('button', { name: 'Open page 7 in the PDF reader' }));

    expect(sectionPdf.scrollIntoView).toHaveBeenCalled();
    expect(goto).toHaveBeenCalledTimes(1);
    expect((goto.mock.calls[0]![0] as CustomEvent).detail).toEqual({
      page: 7,
      quote: 'A supporting quote.',
    });

    window.removeEventListener(PDF_GOTO_EVENT, goto);
  });

  it('passage chip targets and expands the stable chunk anchor', async () => {
    const chunks = [chunk({ id: 99, chunk_index: 0 }), chunk()];
    render(
      <MemoryRouter>
        <EvidenceTab summary={summary([keyFinding()])} chunks={chunks} paperId={42} />
        <section id="section-chunks">
          <ChunksTab chunks={chunks} />
        </section>
      </MemoryRouter>,
    );

    const passage = document.getElementById(passageAnchorId(chunk().id));
    expect(passage).toHaveAttribute('data-chunk-index', String(chunk().chunk_index));
    expect(passage).not.toBeNull();
    passage!.scrollIntoView = vi.fn();

    const chip = screen.getByRole('button', { name: 'Open passage 2 of 2' });
    expect(chip).toHaveTextContent('Passage 2 of 2');
    expect(chip).not.toHaveTextContent(String(keyFinding().chunk_id));

    await userEvent.click(chip);

    expect(passage!.scrollIntoView).toHaveBeenCalled();
    expect(screen.getByText('A supporting quote in its full passage.')).toBeInTheDocument();
    expect(screen.getByText('Passage 2 of 2 (Page 7)')).toBeInTheDocument();
  });

  it('opens the passage collection before revealing a lazy row', async () => {
    sectionChunks.remove();
    const chunks = [chunk({ id: 99, chunk_index: 0 }), chunk()];
    const scrolledIds: string[] = [];
    const originalScroll = HTMLElement.prototype.scrollIntoView;
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value(this: HTMLElement) {
        scrolledIds.push(this.id);
      },
    });
    const animationFrame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      window.setTimeout(() => callback(0), 0);
      return 1;
    });

    try {
      render(
        <MemoryRouter>
          <EvidenceTab summary={summary([keyFinding()])} chunks={chunks} paperId={42} />
          <LazyPassages chunks={chunks} />
        </MemoryRouter>,
      );

      await userEvent.click(screen.getByRole('button', { name: 'Open passage 2 of 2' }));

      await waitFor(() => {
        expect(screen.getByText('A supporting quote in its full passage.')).toBeInTheDocument();
      });
      expect(scrolledIds).toContain(passageAnchorId(chunk().id));
    } finally {
      animationFrame.mockRestore();
      if (originalScroll) {
        Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
          configurable: true,
          value: originalScroll,
        });
      } else {
        Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
      }
    }
  });

  it('states why the page anchor cannot act when the PDF is not downloaded', async () => {
    const goto = vi.fn();
    window.addEventListener(PDF_GOTO_EVENT, goto);
    renderTab(summary([keyFinding()]), 42, false);

    expect(
      screen.queryByRole('button', { name: 'Open page 7 in the PDF reader' }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/Page 7 — download the PDF to open it here/)).toBeInTheDocument();
    expect(goto).not.toHaveBeenCalled();

    window.removeEventListener(PDF_GOTO_EVENT, goto);
  });
});
