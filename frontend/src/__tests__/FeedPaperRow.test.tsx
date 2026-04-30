import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FeedPaperRow } from '@/components/feed/FeedPaperRow';
import type { FeedPaper } from '@/types';

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
  pdf_downloaded: true,
  citation_count: 5,
  priority_score: 0.75,
  metadata: {},
  discovered_at: '2026-01-02T00:00:00Z',
  created_at: '2026-01-02T00:00:00Z',
  summary_brief: 'Brief summary',
  tldr: null,
  confidence: 'HIGH',
  user_status: 'new',
  rating: null,
  has_chunks: true,
  has_summary: false,
};

const paperWithUserState: FeedPaper = {
  ...paper,
  id: 8,
  title: 'UserState Paper',
  user_state: {
    saved: true,
    starred: true,
    status: 'read',
    dismissed: false,
    archived: false,
    preference: 'none',
    rating: null,
    user_notes: null,
    flagged: false,
    updated_at: null,
  },
};

describe('FeedPaperRow', () => {
  it('renders shared metadata and optional row actions', async () => {
    const user = userEvent.setup();
    const onSeedChange = vi.fn();
    const onMarkRead = vi.fn();
    const onArchive = vi.fn();
    const onView = vi.fn();

    render(
      <FeedPaperRow
        paper={paper}
        seedChecked={false}
        onSeedChange={onSeedChange}
        onMarkRead={onMarkRead}
        onArchive={onArchive}
        onView={onView}
      />,
    );

    expect(screen.getByText('Shared Feed Row Paper')).toBeInTheDocument();
    expect(screen.getByText('Ada Lovelace, Grace Hopper')).toBeInTheDocument();
    expect(screen.getByText('ARXIV')).toBeInTheDocument();

    await user.click(screen.getByLabelText('Select Shared Feed Row Paper as seed'));
    await user.click(screen.getByRole('button', { name: 'Mark Shared Feed Row Paper as read' }));
    await user.click(screen.getByRole('button', { name: 'Archive Shared Feed Row Paper' }));
    await user.click(screen.getByRole('button', { name: 'View Shared Feed Row Paper details' }));

    expect(onSeedChange).toHaveBeenCalledWith(7);
    expect(onMarkRead).toHaveBeenCalledWith(7);
    expect(onArchive).toHaveBeenCalledWith(7);
    expect(onView).toHaveBeenCalledWith(7);
  });

  it('disables the archive action while the archive mutation is pending', () => {
    render(
      <FeedPaperRow
        paper={paper}
        onArchive={vi.fn()}
        archivePending
      />,
    );

    expect(screen.getByRole('button', { name: 'Archive Shared Feed Row Paper' })).toBeDisabled();
  });

  it('renders the recommendation badge with a star glyph (Sprint 7 B14)', () => {
    const recommendedPaper: FeedPaper = {
      ...paper,
      recommendation_score: 0.92,
      recommendation_reason: 'Matches your topic profile',
    };
    render(<FeedPaperRow paper={recommendedPaper} />);
    expect(
      screen.getByText(/★\s*Matches your topic profile/),
    ).toBeInTheDocument();
  });

  it('exposes published date in a tooltip when discovered_at differs (Sprint 7 B15)', () => {
    const dualDatePaper: FeedPaper = {
      ...paper,
      discovered_at: '2026-04-29T00:00:00Z',
      published_date: '2025-12-01',
    };
    const { container } = render(<FeedPaperRow paper={dualDatePaper} />);
    const dateSpan = container.querySelector('span[title]');
    expect(dateSpan).not.toBeNull();
    expect(dateSpan?.getAttribute('title')).toMatch(/Published:/);
  });

  it('renders NEW badge when user_status is unset (Sprint 7 B17)', () => {
    const unsetPaper: FeedPaper = { ...paper, user_status: null };
    render(<FeedPaperRow paper={unsetPaper} />);
    // The component renders both a NEW Badge and a status badge (both show "NEW"),
    // so we assert at least one is present.
    expect(screen.getAllByText('NEW').length).toBeGreaterThanOrEqual(1);
  });

  // ── Sprint 8 B3.7 tests ────────────────────────────────────────────────────

  it('reads user_state booleans: shows ★ when starred=true and status=read badge', () => {
    render(<FeedPaperRow paper={paperWithUserState} />);
    // ★ glyph is rendered inside a span when isStarred is true
    expect(screen.getByTitle('Starred')).toBeInTheDocument();
    // status badge should show READ (uppercased)
    expect(screen.getByText('READ')).toBeInTheDocument();
  });

  it('surface-aware: shows Save button when onSave is provided and calls it on click', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<FeedPaperRow paper={paper} onSave={onSave} />);
    const saveBtn = screen.getByRole('button', { name: `Save ${paper.title}` });
    expect(saveBtn).toBeInTheDocument();
    await user.click(saveBtn);
    expect(onSave).toHaveBeenCalledWith(paper.id);
  });

  it('surface-aware: shows Restore and HardDelete buttons when those callbacks are provided', () => {
    const onRestore = vi.fn();
    const onHardDelete = vi.fn();
    render(
      <FeedPaperRow
        paper={paper}
        onRestore={onRestore}
        onHardDelete={onHardDelete}
      />,
    );
    expect(
      screen.getByRole('button', { name: `Restore ${paper.title}` }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: `Permanently delete ${paper.title}` }),
    ).toBeInTheDocument();
  });

  it('bulk checkbox toggles when onBulkToggle is provided and calls it on click', async () => {
    const user = userEvent.setup();
    const onBulkToggle = vi.fn();
    render(
      <FeedPaperRow paper={paper} onBulkToggle={onBulkToggle} bulkSelected={false} />,
    );
    const checkbox = screen.getByRole('checkbox', {
      name: `Select ${paper.title} for bulk action`,
    });
    await user.click(checkbox);
    expect(onBulkToggle).toHaveBeenCalledWith(paper.id);
  });

  it('omits action buttons whose callback is not passed', () => {
    render(<FeedPaperRow paper={paper} onView={vi.fn()} />);
    // Only View button should be present; Save/Star/Archive/Dismiss must be absent
    expect(
      screen.queryByRole('button', { name: `Save ${paper.title}` }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: `Archive ${paper.title}` }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: `Dismiss ${paper.title}` }),
    ).not.toBeInTheDocument();
    // View button IS present
    expect(
      screen.getByRole('button', { name: `View ${paper.title} details` }),
    ).toBeInTheDocument();
  });
});
