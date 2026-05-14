import { describe, it, expect, vi } from 'vitest';
import { render, screen, act } from '@testing-library/react';
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

  it('bulk checkbox toggles when onBulkToggle is provided and calls it on click', async () => {
    const user = userEvent.setup();
    const onBulkToggle = vi.fn();
    renderRow({ paper, onBulkToggle, bulkSelected: false });
    const checkbox = screen.getByRole('checkbox', {
      name: `Select ${paper.title} for bulk action`,
    });
    await user.click(checkbox);
    expect(onBulkToggle).toHaveBeenCalledWith(paper.id);
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
});

// DOM-F-02: verify FeedPaperRow is wrapped in React.memo so unrelated rows do
// not re-render when sibling state changes.
describe('FeedPaperRow memoization', () => {
  it('test_feed_paper_row_is_memoized: is exported as a React.memo component', () => {
    // React.memo wraps the inner function and sets $$typeof to Symbol(react.memo).
    // This is the canonical way to assert memoization without depending on
    // render-count infrastructure.
    const memoType = (React.memo(() => null) as { $$typeof: symbol }).$$typeof;
    const rowType = (FeedPaperRow as unknown as { $$typeof: symbol }).$$typeof;
    expect(rowType).toBe(memoType);
  });

  it('does not re-render an unselected row when a sibling row changes selection', () => {
    // Render two rows in a parent that controls isSelected independently.
    let renderCountA = 0;

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });

    const paperA: FeedPaper = { ...paper, id: 100, title: 'Row A' };
    const paperB: FeedPaper = { ...paper, id: 101, title: 'Row B' };

    // Spy on FeedPaperRow renders by wrapping in a thin counter component.
    function RowWithCount({
      p,
      isSelected,
      counter,
    }: {
      p: FeedPaper;
      isSelected: boolean;
      counter: { count: number };
    }) {
      counter.count++;
      return (
        <FeedPaperRow
          paper={p}
          isSelected={isSelected}
          onToggleSelect={vi.fn()}
        />
      );
    }

    const counterA = { count: 0 };
    const counterB = { count: 0 };

    function Parent({ selectedB }: { selectedB: boolean }) {
      return (
        <QueryClientProvider client={client}>
          <RowWithCount p={paperA} isSelected={false} counter={counterA} />
          <RowWithCount p={paperB} isSelected={selectedB} counter={counterB} />
        </QueryClientProvider>
      );
    }

    const { rerender } = render(<Parent selectedB={false} />);
    const initialCountA = counterA.count;
    const initialCountB = counterB.count;

    // Change only row B's isSelected — row A should not re-render.
    act(() => {
      rerender(<Parent selectedB={true} />);
    });

    // Row A must not have re-rendered (count unchanged since initial render).
    // Row B should have re-rendered because its prop changed.
    expect(counterA.count).toBe(initialCountA);
    expect(counterB.count).toBeGreaterThan(initialCountB);
  });
});
