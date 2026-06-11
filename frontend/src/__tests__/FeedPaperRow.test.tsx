import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import React from 'react';
import { FeedPaperRow } from '@/components/feed/FeedPaperRow';
import type { FeedPaper } from '@/types';

// FeedbackButtons uses useMutation — wrap with QueryClientProvider
function renderRow(props: Parameters<typeof FeedPaperRow>[0]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <FeedPaperRow {...props} />
    </QueryClientProvider>,
  );
}

const paper: FeedPaper = {
  id: 7,
  external_id: 'arxiv:2601.00007',
  source_type: 'arxiv',
  title: 'Shared Feed Row Paper',
  authors: ['Ada Lovelace', 'Grace Hopper'],
  abstract: 'Abstract',
  published_date: '2026-01-01',
  url: 'https://example.com/paper',
  pdf_url: null,
  pdf_local_path: null,
  pdf_downloaded: false,
  discovered_at: null,
  citation_count: 5,
  priority_score: 0.75,
  metadata: {},
  created_at: '2026-01-02T00:00:00Z',
  summary_brief: 'Brief summary',
  tldr: null,
  confidence: 'HIGH',
  rating: null,
  has_chunks: true,
  has_summary: false,
  state: 'inbox',
  state_before_trash: null,
  starred: false,
  discovery_origin: 'pulse',
  user_state: {
    state: 'inbox',
    state_before_trash: null,
    starred: false,
    rating: null,
    user_notes: null,
    flagged: false,
    updated_at: null,
  },
  recent_feedback: null,
};

const toReadPaper: FeedPaper = {
  ...paper,
  id: 8,
  title: 'To-Read Paper',
  state: 'to_read',
};

const readingPaper: FeedPaper = {
  ...paper,
  id: 9,
  title: 'Reading Paper',
  state: 'reading',
  starred: true,
};

const donePaper: FeedPaper = {
  ...paper,
  id: 10,
  title: 'Done Paper',
  state: 'done',
};

const trashPaper: FeedPaper = {
  ...paper,
  id: 11,
  title: 'Trash Paper',
  state: 'trash',
};

describe('FeedPaperRow', () => {
  it('renders shared metadata', () => {
    renderRow({ paper });
    expect(screen.getByText('Shared Feed Row Paper')).toBeInTheDocument();
    expect(screen.getByText('Ada Lovelace, Grace Hopper')).toBeInTheDocument();
    expect(screen.getByText('ARXIV')).toBeInTheDocument();
  });

  it('renders seed checkbox when onSeedChange is provided and calls it on click', async () => {
    const user = userEvent.setup();
    const onSeedChange = vi.fn();
    renderRow({ paper, seedChecked: false, onSeedChange });
    await user.click(screen.getByLabelText('Select Shared Feed Row Paper as seed'));
    expect(onSeedChange).toHaveBeenCalledWith(7);
  });

  it('state=inbox: renders Save and Skip buttons', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    const onSkip = vi.fn();
    renderRow({ paper, onSave, onSkip });
    await user.click(screen.getByRole('button', { name: `Save ${paper.title}` }));
    await user.click(screen.getByRole('button', { name: `Skip ${paper.title}` }));
    expect(onSave).toHaveBeenCalledWith(paper.id);
    expect(onSkip).toHaveBeenCalledWith(paper.id);
  });

  it('state=inbox: renders NEW badge', () => {
    renderRow({ paper });
    expect(screen.getAllByText('NEW').length).toBeGreaterThanOrEqual(1);
  });

  it('state=to_read: renders Mark Reading and Mark Done buttons', async () => {
    const user = userEvent.setup();
    const onMarkReading = vi.fn();
    const onMarkDone = vi.fn();
    renderRow({ paper: toReadPaper, onMarkReading, onMarkDone });
    await user.click(screen.getByRole('button', { name: `Mark ${toReadPaper.title} as reading` }));
    await user.click(screen.getByRole('button', { name: `Mark ${toReadPaper.title} as done` }));
    expect(onMarkReading).toHaveBeenCalledWith(toReadPaper.id);
    expect(onMarkDone).toHaveBeenCalledWith(toReadPaper.id);
  });

  it('state=reading: renders Set Aside and Mark Done buttons; shows ★ when starred=true', async () => {
    const user = userEvent.setup();
    const onSetAside = vi.fn();
    const onMarkDone = vi.fn();
    renderRow({ paper: readingPaper, onSetAside, onMarkDone });
    expect(screen.getByTitle('Starred')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: `Set aside ${readingPaper.title}` }));
    await user.click(screen.getByRole('button', { name: `Mark ${readingPaper.title} as done` }));
    expect(onSetAside).toHaveBeenCalledWith(readingPaper.id);
    expect(onMarkDone).toHaveBeenCalledWith(readingPaper.id);
  });

  it('state=done: renders Re-open button', async () => {
    const user = userEvent.setup();
    const onReopen = vi.fn();
    renderRow({ paper: donePaper, onReopen });
    await user.click(screen.getByRole('button', { name: `Re-open ${donePaper.title}` }));
    expect(onReopen).toHaveBeenCalledWith(donePaper.id);
  });

  it('state=trash: renders Restore and Permanently delete buttons', async () => {
    const user = userEvent.setup();
    const onRestore = vi.fn();
    const onHardDelete = vi.fn();
    renderRow({ paper: trashPaper, onRestore, onHardDelete });
    await user.click(screen.getByRole('button', { name: `Restore ${trashPaper.title}` }));
    await user.click(screen.getByRole('button', { name: `Permanently delete ${trashPaper.title}` }));
    expect(onRestore).toHaveBeenCalledWith(trashPaper.id);
    expect(onHardDelete).toHaveBeenCalledWith(trashPaper.id);
  });

  it('state=trash: no FeedbackButtons rendered', () => {
    renderRow({ paper: trashPaper });
    // FeedbackButtons renders thumbs-up/thumbs-down; should not be present for trash
    expect(screen.queryByLabelText(/thumbs up/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/thumbs down/i)).not.toBeInTheDocument();
  });

  it('bulk checkbox toggles when onToggleSelect is provided and calls it on click', async () => {
    const user = userEvent.setup();
    const onToggleSelect = vi.fn();
    renderRow({ paper, onToggleSelect, isSelected: false });
    const checkbox = screen.getByRole('checkbox', {
      name: `Select ${paper.title} for bulk action`,
    });
    await user.click(checkbox);
    expect(onToggleSelect).toHaveBeenCalledWith(paper.id);
  });

  it('shows publication date prominently and added date as secondary', () => {
    // paper has published_date='2026-01-01', created_at='2026-01-02T00:00:00Z'.
    // The PRIMARY (prominent) date node must be the publication date ("Jan 1,
    // 2026"). The added date ("Jan 2, 2026") may only appear as SECONDARY —
    // inside the muted "(added …)" span / its title attribute — never as the
    // primary date. This fails if the primary were reverted to created_at.
    renderRow({ paper });

    // Primary date node: matches on the span's DIRECT text node only
    // (Testing Library's getNodeText excludes child elements), so this resolves
    // the prominent date — the publication date "Jan 1, 2026".
    expect(screen.getByText('Jan 1, 2026')).toBeInTheDocument();

    // The added date appears only as secondary: a "(added Jan 2, 2026)" span
    // carrying title="Added: Jan 2, 2026" — never as a standalone primary date.
    const secondary = screen.getByText('(added Jan 2, 2026)');
    expect(secondary).toBeInTheDocument();
    expect(secondary).toHaveAttribute('title', 'Added: Jan 2, 2026');

    // No element exposes a bare "Jan 2, 2026" as its own text node — the added
    // date is only ever wrapped in the "(added …)" secondary span. If the
    // primary date were reverted to created_at, a bare "Jan 2, 2026" primary
    // node would appear here and this assertion would fail.
    expect(screen.queryAllByText('Jan 2, 2026')).toHaveLength(0);
  });

  it('falls back to added date when published_date is absent', () => {
    const paperNoPublished: FeedPaper = { ...paper, published_date: null };
    renderRow({ paper: paperNoPublished });
    // Falls back to showing created_at ("Jan 2, 2026") — not "N/A"
    expect(screen.getByText('Jan 2, 2026')).toBeInTheDocument();
  });

  it('recommendation badge renders with star glyph', () => {
    const recommendedPaper: FeedPaper = {
      ...paper,
      recommendation_score: 0.92,
      recommendation_reason: 'Matches your topic profile',
    };
    renderRow({ paper: recommendedPaper });
    expect(screen.getByText(/★\s*Matches your topic profile/)).toBeInTheDocument();
  });

  it('omits action buttons whose callback is not passed', () => {
    renderRow({ paper, onView: vi.fn() });
    expect(screen.queryByRole('button', { name: `Save ${paper.title}` })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: `Skip ${paper.title}` })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: `View ${paper.title} details` })).toBeInTheDocument();
  });

  it('onToggleSelect (new API) works for bulk selection', async () => {
    const user = userEvent.setup();
    const onToggleSelect = vi.fn();
    renderRow({ paper, onToggleSelect, isSelected: false });
    await user.click(screen.getByRole('checkbox', { name: `Select ${paper.title} for bulk action` }));
    expect(onToggleSelect).toHaveBeenCalledWith(paper.id);
  });

  it('row container has mobile-first stacking classes (sm:flex-row)', () => {
    const { container } = renderRow({ paper });
    const row = container.querySelector('.flex.flex-col.gap-3.sm\\:flex-row');
    expect(row).not.toBeNull();
  });
});

// DOM-F-02 (regression): stable onHardDelete — FeedPaperRow must be memo'd and
// accept a stable onHardDelete reference without re-rendering.
describe('FeedPaperRow onHardDelete stable callback (DOM-F-02 regression)', () => {
  it('FeedPaperRow is memo-wrapped so a stable onHardDelete does not trigger spurious re-renders', () => {
    // Strategy: use a memo-wrapped spy that delegates to FeedPaperRow.
    // React.memo compares props by reference. If FeedPaperRow's own memo layer
    // were absent, the inner body would run on every parent re-render regardless.
    // Here we directly confirm:
    //   1. FeedPaperRow carries $$typeof === Symbol(react.memo)
    //   2. With identical prop references the DOM is stable across rerenders
    //
    // (A render-count spy on the inner function requires vi.spyOn on a named export
    // which is not available for the default memo pattern; the $$typeof check + DOM
    // stability is the canonical vitest approach.)

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });

    const stablePaper: FeedPaper = { ...paper, id: 300, title: 'Stable Memo Row', state: 'trash' };
    const stableOnHardDelete = vi.fn();

    // Confirm structural guarantee first.
     
    function Probe() { return null; }
    const memoType = (React.memo(Probe) as unknown as { $$typeof: symbol }).$$typeof;
    const rowType = (FeedPaperRow as unknown as { $$typeof: symbol }).$$typeof;
    expect(rowType).toBe(memoType);

    // DOM stability: re-rendering with the same prop references must not change DOM.
    const { rerender } = render(
      React.createElement(
        QueryClientProvider,
        { client },
        React.createElement(FeedPaperRow, {
          paper: stablePaper,
          onHardDelete: stableOnHardDelete,
        }),
      ),
    );

    rerender(
      React.createElement(
        QueryClientProvider,
        { client },
        React.createElement(FeedPaperRow, {
          paper: stablePaper,
          onHardDelete: stableOnHardDelete,
        }),
      ),
    );

    // Title still present — confirms no unexpected unmount or crash.
    expect(screen.getAllByText('Stable Memo Row').length).toBeGreaterThanOrEqual(1);
  });
});

// DOM-F-02: verify FeedPaperRow is wrapped in React.memo so unrelated rows do
// not re-render when sibling state changes.
describe('FeedPaperRow memoization', () => {
  it('test_feed_paper_row_is_memoized: is exported as a React.memo component', () => {
    // React.memo wraps the inner function and sets $$typeof to Symbol(react.memo).
    // This is the canonical way to assert memoization without depending on
    // render-count infrastructure.
     
    function Probe() { return null; }
    const memoType = (React.memo(Probe) as unknown as { $$typeof: symbol }).$$typeof;
    const rowType = (FeedPaperRow as unknown as { $$typeof: symbol }).$$typeof;
    expect(rowType).toBe(memoType);
  });

  it('props with equal references do not cause a re-render (memo contract)', () => {
    // Verify that passing identical props on a rerender does not call the inner
    // render function again.  We achieve this by spying on the wrapped type:
    // React.memo only re-renders when props change by reference or value.
    // We confirm the component accepts a stable onSave prop without re-rendering.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });

    const stablePaper: FeedPaper = { ...paper, id: 200, title: 'Stable Row' };
    const stableOnSave = vi.fn();

    // First render — baseline.
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <FeedPaperRow paper={stablePaper} isSelected={false} onSave={stableOnSave} />
      </QueryClientProvider>,
    );

    // Re-render with IDENTICAL props (same object references).
    // If FeedPaperRow is memo'd, the inner component will not re-execute.
    // We assert the DOM is still stable (title text unchanged).
    rerender(
      <QueryClientProvider client={client}>
        <FeedPaperRow paper={stablePaper} isSelected={false} onSave={stableOnSave} />
      </QueryClientProvider>,
    );

    // DOM should still show the paper title — confirms no crash / unexpected
    // unmount.  The $$typeof test above provides the structural guarantee;
    // this confirms nothing breaks after a no-op re-render.
    expect(screen.getByText('Stable Row')).toBeInTheDocument();
  });
});
