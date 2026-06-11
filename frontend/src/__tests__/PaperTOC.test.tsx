/**
 * PaperTOC.test.tsx
 * Tests for the left-rail TOC + pipeline status component.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
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
    expect(screen.getByText('42 passages')).toBeInTheDocument();
  });

  it('shows Summarized when hasSummary=true', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 10, hasSummary: true });
    expect(screen.getByText('Summarized')).toBeInTheDocument();
  });

  it('pipeline status: full pipeline complete', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 5, hasSummary: true });
    expect(screen.getByText('Downloaded')).toBeInTheDocument();
    expect(screen.getByText('5 passages')).toBeInTheDocument();
    expect(screen.getByText('Summarized')).toBeInTheDocument();
  });
});

describe('PaperTOC — pipeline tri-state icons', () => {
  // Helpers — lucide icons render an <svg> with a title equal to the component display name.
  // We locate icon SVGs by querying the closest flex-row container for each step label.

  function getStepRow(label: string | RegExp) {
    return screen.getByText(label).closest('div.flex') as HTMLElement;
  }

  it('done step renders CheckCircle2 icon (text-[var(--status-ok)])', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 0, hasSummary: false });
    const row = getStepRow('Downloaded');
    // Lucide icons render as <svg>; the done step has exactly one SVG (CheckCircle2).
    const svgs = row.querySelectorAll('svg');
    expect(svgs.length).toBe(1);
     
    const svg = svgs[0]!;
    // CheckCircle2 carries the ok-status colour class.
    const iconClass = svg.className.baseVal ?? svg.getAttribute('class') ?? '';
    expect(iconClass).toContain('text-[var(--status-ok)]');
    // The step label has line-through when done.
    const span = within(row).getByText('Downloaded');
    expect(span.className).toContain('line-through');
  });

  it('active (in-progress) step renders spinning Loader2 icon', () => {
    // PDF downloaded, no chunks yet → Processing… is active.
    renderTOC({ pdfDownloaded: true, chunkCount: 0, hasSummary: false });
    const row = getStepRow('Processing…');
    const svg = row.querySelector('svg');
    expect(svg).not.toBeNull();
    // Loader2 has the animate-spin class applied to it.
    expect(svg!.className.baseVal ?? svg!.getAttribute('class')).toMatch(/animate-spin/);
  });

  it('failed step renders XCircle icon with destructive styling', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 0, hasSummary: false, processingFailed: true });
    const row = getStepRow('Processing…');
    const svg = row.querySelector('svg');
    expect(svg).not.toBeNull();
    // XCircle should carry text-destructive.
    const iconClass = svg!.className.baseVal ?? svg!.getAttribute('class') ?? '';
    expect(iconClass).toContain('text-destructive');
    // The step label itself should also be styled destructive + font-medium.
    const span = within(row).getByText('Processing…');
    expect(span.className).toContain('text-destructive');
    expect(span.className).toContain('font-medium');
  });

  it('pending step renders empty circle div (no SVG)', () => {
    // Nothing downloaded → all three steps pending.
    renderTOC({ pdfDownloaded: false, chunkCount: 0, hasSummary: false });
    const row = getStepRow('Downloaded');
    // Pending uses a plain <div> with rounded-full border, not an SVG icon.
    const svg = row.querySelector('svg');
    expect(svg).toBeNull();
    const placeholder = row.querySelector('div.rounded-full');
    expect(placeholder).not.toBeNull();
  });

  it('processingFailed=false does NOT show failed icon on processing step', () => {
    renderTOC({ pdfDownloaded: true, chunkCount: 0, hasSummary: false, processingFailed: false });
    const row = getStepRow('Processing…');
    const svg = row.querySelector('svg');
    // Active (Loader2) — should have animate-spin, not text-destructive.
    const iconClass = svg ? (svg.className.baseVal ?? svg.getAttribute('class') ?? '') : '';
    expect(iconClass).not.toContain('text-destructive');
  });
});
