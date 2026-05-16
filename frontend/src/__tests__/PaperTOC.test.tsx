/**
 * PaperTOC.test.tsx
 * Tests for the left-rail TOC + pipeline status component.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { PaperTOC, type TOCSection, type PipelineStatus } from '@/components/paper/PaperTOC';

const SECTIONS: TOCSection[] = [
  { id: 'section-brief', label: 'Brief' },
  { id: 'section-findings', label: 'Evidence', count: 3 },
  { id: 'section-crossrefs', label: 'Cross-references', count: 0 },
  { id: 'section-notes', label: 'Your Notes', count: 2 },
];

function renderTOC(
  pipeline: PipelineStatus,
  activeId: string | null = 'section-brief',
  onNavigate = vi.fn(),
) {
  return render(
    <PaperTOC
      sections={SECTIONS}
      activeId={activeId}
      pipeline={pipeline}
      onNavigate={onNavigate}
    />,
  );
}

describe('PaperTOC — sections', () => {
  it('renders all section labels', () => {
    renderTOC({ pdfDownloaded: false, chunkCount: 0, hasSummary: false });
    expect(screen.getByText('Brief')).toBeInTheDocument();
    expect(screen.getByText('Evidence')).toBeInTheDocument();
    expect(screen.getByText('Cross-references')).toBeInTheDocument();
    expect(screen.getByText('Your Notes')).toBeInTheDocument();
  });

  it('shows count badge when count > 0', () => {
    renderTOC({ pdfDownloaded: false, chunkCount: 0, hasSummary: false });
    // Evidence has count=3
    expect(screen.getByText('3')).toBeInTheDocument();
    // Your Notes has count=2
    expect(screen.getByText('2')).toBeInTheDocument();
    // Cross-references has count=0 — badge NOT rendered
    const badges = screen.queryAllByText('0');
    expect(badges).toHaveLength(0);
  });

  it('marks active section with aria-current="location"', () => {
    renderTOC({ pdfDownloaded: false, chunkCount: 0, hasSummary: false }, 'section-findings');
    const activeBtn = screen.getByRole('button', { name: /Evidence/ });
    expect(activeBtn).toHaveAttribute('aria-current', 'location');
  });

  it('does not mark inactive sections with aria-current', () => {
    renderTOC({ pdfDownloaded: false, chunkCount: 0, hasSummary: false }, 'section-brief');
    const evidenceBtn = screen.getByRole('button', { name: /Evidence/ });
    expect(evidenceBtn).not.toHaveAttribute('aria-current');
  });

  it('calls onNavigate with the correct id when a TOC item is clicked', async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    renderTOC({ pdfDownloaded: false, chunkCount: 0, hasSummary: false }, 'section-brief', onNavigate);

    await user.click(screen.getByRole('button', { name: /Evidence/ }));
    expect(onNavigate).toHaveBeenCalledWith('section-findings');
  });
});

describe('PaperTOC — pipeline status', () => {
  it('shows all steps as pending when nothing is done', () => {
    renderTOC({ pdfDownloaded: false, chunkCount: 0, hasSummary: false });
    // All three step labels present
    expect(screen.getByText('Downloaded')).toBeInTheDocument();
    expect(screen.getByText('Processing…')).toBeInTheDocument();
    expect(screen.getByText('Summarizing…')).toBeInTheDocument();
  });

  it('shows Downloaded as complete when pdfDownloaded=true', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 0, hasSummary: false });
    expect(screen.getByText('Downloaded')).toBeInTheDocument();
  });

  it('shows chunk count when chunks > 0', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 42, hasSummary: false });
    expect(screen.getByText('42 chunks')).toBeInTheDocument();
  });

  it('shows Summarized when hasSummary=true', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 10, hasSummary: true });
    expect(screen.getByText('Summarized')).toBeInTheDocument();
  });

  it('pipeline status: full pipeline complete', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 5, hasSummary: true });
    expect(screen.getByText('Downloaded')).toBeInTheDocument();
    expect(screen.getByText('5 chunks')).toBeInTheDocument();
    expect(screen.getByText('Summarized')).toBeInTheDocument();
  });
});
