import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { EvidenceTab } from '@/components/paper/EvidenceTab';
import { PDF_GOTO_EVENT } from '@/lib/pdf-events';
import type { KeyFinding, Summary } from '@/types';

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

  it('passage anchor scrolls to the source passages section', async () => {
    renderTab(summary([keyFinding()]), 42);

    const anchor = screen.getByRole('button', { name: 'Open the source passages section' });
    // The visible label promises the whole section, which is what the click
    // delivers — never a jump to one numbered passage.
    expect(anchor).toHaveTextContent('Source passages');
    expect(anchor).not.toHaveTextContent(String(keyFinding().chunk_id));

    await userEvent.click(anchor);

    expect(sectionChunks.scrollIntoView).toHaveBeenCalled();
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
