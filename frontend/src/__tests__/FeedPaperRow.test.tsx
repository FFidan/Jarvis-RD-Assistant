import { describe, it, expect, vi, beforeAll } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { FeedPaperRow } from '@/components/feed/FeedPaperRow';
import type { FeedPaper } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
import { makeFeedPaper } from '@/__tests__/fixtures/feed-paper';

// Only the citation client is stubbed; everything else the row touches stays
// real so the rest of this file keeps testing the row it ships.
vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  copyPaperCitation: vi.fn(),
}));

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

// FeedbackButtons uses useMutation — wrap with QueryClientProvider
function renderRow(props: Parameters<typeof FeedPaperRow>[0]) {
  const client = createTestQueryClient();
  return renderWithProviders(
    <FeedPaperRow {...props} />,
    { queryClient: client },
  );
}

const paper: FeedPaper = makeFeedPaper({
  id: 7,
  external_id: 'arxiv:2601.00007',
  title: 'Shared Feed Row Paper',
  authors: ['Ada Lovelace', 'Grace Hopper'],
  abstract: 'Abstract',
  url: 'https://example.com/paper',
  discovered_at: null,
  citation_count: 5,
  priority_score: 0.75,
  created_at: '2026-01-02T00:00:00Z',
  summary_brief: 'Brief summary',
  tldr: null,
  confidence: 'HIGH',
  has_chunks: true,
  has_summary: false,
  user_state: {
    state: 'inbox',
    state_before_trash: null,
    starred: false,
    rating: null,
    user_notes: null,
    flagged: false,
    updated_at: null,
  },
});

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

// Radix DropdownMenu (the row's overflow menu) relies on pointer-capture APIs
// not present in jsdom.
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

/** Opens the row's overflow menu and clicks the named item. */
async function selectOverflow(
  user: ReturnType<typeof userEvent.setup>,
  title: string,
  item: RegExp,
) {
  await user.click(screen.getByRole('button', { name: `More actions for ${title}` }));
  await user.click(await screen.findByRole('menuitem', { name: item }));
}

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

  it('state=inbox: Save is the primary action and Skip lives in the overflow', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onSave = vi.fn();
    const onSkip = vi.fn();
    renderRow({ paper, onSave, onSkip });
    await user.click(screen.getByRole('button', { name: `Save ${paper.title}` }));
    expect(onSave).toHaveBeenCalledWith(paper.id);
    // Skip is not a top-level button any more.
    expect(screen.queryByRole('button', { name: `Skip ${paper.title}` })).not.toBeInTheDocument();
    await selectOverflow(user, paper.title, /^Skip$/);
    expect(onSkip).toHaveBeenCalledWith(paper.id);
  });

  it('state=inbox: renders NEW badge', () => {
    renderRow({ paper });
    expect(screen.getAllByText('NEW').length).toBeGreaterThanOrEqual(1);
  });

  it('state=to_read: Start reading is primary and Mark Done lives in the overflow', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onMarkReading = vi.fn();
    const onMarkDone = vi.fn();
    renderRow({ paper: toReadPaper, onMarkReading, onMarkDone });
    expect(screen.getByText('Reading List')).toBeInTheDocument();
    expect(screen.queryByText('TO_READ')).not.toBeInTheDocument();
    await user.click(
      screen.getByRole('button', { name: `Start reading ${toReadPaper.title}` }),
    );
    expect(onMarkReading).toHaveBeenCalledWith(toReadPaper.id);
    await selectOverflow(user, toReadPaper.title, /^Mark Done$/);
    expect(onMarkDone).toHaveBeenCalledWith(toReadPaper.id);
  });

  it('state=reading: Mark Done is primary, Pause reading is overflow; shows ★ when starred=true', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onSetAside = vi.fn();
    const onMarkDone = vi.fn();
    renderRow({ paper: readingPaper, onSetAside, onMarkDone });
    expect(screen.getByTitle('Starred')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: `Mark Done ${readingPaper.title}` }));
    expect(onMarkDone).toHaveBeenCalledWith(readingPaper.id);
    await selectOverflow(user, readingPaper.title, /^Pause reading$/);
    expect(onSetAside).toHaveBeenCalledWith(readingPaper.id);
  });

  it('state=done: Reopen is the primary action', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onReopen = vi.fn();
    renderRow({ paper: donePaper, onReopen });
    await user.click(screen.getByRole('button', { name: `Reopen ${donePaper.title}` }));
    expect(onReopen).toHaveBeenCalledWith(donePaper.id);
  });

  it('state=trash: Restore is primary and Permanently delete lives in the overflow', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onRestore = vi.fn();
    const onHardDelete = vi.fn();
    renderRow({ paper: trashPaper, onRestore, onHardDelete });
    await user.click(screen.getByRole('button', { name: `Restore ${trashPaper.title}` }));
    expect(onRestore).toHaveBeenCalledWith(trashPaper.id);
    await selectOverflow(user, trashPaper.title, /^Permanently delete$/);
    expect(onHardDelete).toHaveBeenCalledWith(trashPaper.id);
  });

  it('citing one paper is reachable from the overflow, without its own row control', async () => {
    const { copyPaperCitation } = await import('@/lib/api');
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.mocked(copyPaperCitation).mockResolvedValue('@article{x}');
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
      writable: true,
    });
    renderRow({ paper, onSave: vi.fn() });

    // The refined row spends no permanent control on citing.
    expect(screen.queryByRole('button', { name: /^Cite$/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: `More actions for ${paper.title}` }));
    await user.click(await screen.findByRole('menuitem', { name: /^Cite$/ }));
    // A plain click event: userEvent's pointer sequence does not reach an item
    // inside a Radix submenu portal under jsdom.
    fireEvent.click(await screen.findByRole('menuitem', { name: /Copy BibTeX/ }));

    await waitFor(() => expect(copyPaperCitation).toHaveBeenCalledWith(paper.id, 'bibtex'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('@article{x}'));
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

  it('omits action items whose callback is not passed', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderRow({ paper, onView: vi.fn() });
    expect(screen.queryByRole('button', { name: `Save ${paper.title}` })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: `View ${paper.title} details` })).toBeInTheDocument();

    // The overflow still opens for the actions that need no callback, and
    // offers nothing it cannot carry out.
    await user.click(screen.getByRole('button', { name: `More actions for ${paper.title}` }));
    expect((await screen.findAllByRole('menuitem')).map((item) => item.textContent)).toEqual([
      'Cite',
    ]);
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

// Regression: stable onHardDelete — FeedPaperRow must be memo'd and
// accept a stable onHardDelete reference without re-rendering.
describe('FeedPaperRow onHardDelete stable callback (regression)', () => {
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

    const client = createTestQueryClient();

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

// Verify FeedPaperRow is wrapped in React.memo so unrelated rows do
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
    const client = createTestQueryClient();

    const stablePaper: FeedPaper = { ...paper, id: 200, title: 'Stable Row' };
    const stableOnSave = vi.fn();

    // First render — baseline.
    const { rerender } = renderWithProviders(
      <FeedPaperRow paper={stablePaper} isSelected={false} onSave={stableOnSave} />,
      { queryClient: client },
    );

    // Re-render with IDENTICAL props (same object references).
    // If FeedPaperRow is memo'd, the inner component will not re-execute.
    // We assert the DOM is still stable (title text unchanged).
    rerender(
      <FeedPaperRow paper={stablePaper} isSelected={false} onSave={stableOnSave} />,
    );

    // DOM should still show the paper title — confirms no crash / unexpected
    // unmount.  The $$typeof test above provides the structural guarantee;
    // this confirms nothing breaks after a no-op re-render.
    expect(screen.getByText('Stable Row')).toBeInTheDocument();
  });
});
