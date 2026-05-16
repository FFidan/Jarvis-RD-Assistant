/**
 * CommandPaletteSearch — F1 global ⌘K command palette tests
 *
 * Coverage:
 *  - ⌘K (the global keydown registered by the controller hook) opens the
 *    palette via the store.
 *  - Esc closes it.
 *  - Typing a query runs the debounced searchPreview call and renders
 *    results (title + authors).
 *  - Selecting a result navigates to /paper/:id and closes the palette.
 *  - A failed search shows the friendly error state (no throw).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { CommandPaletteSearch } from '@/components/layout/CommandPaletteSearch';
import { useCommandPalette } from '@/stores/command-palette-store';
import { searchPreview } from '@/lib/api';
import type { SearchPreviewResponse } from '@/types';

vi.mock('@/lib/api', () => ({
  searchPreview: vi.fn(),
}));

const mockSearchPreview = vi.mocked(searchPreview);

function makeResponse(): SearchPreviewResponse {
  return {
    results: [
      {
        external_id: 'abc.123',
        source_type: 'arxiv',
        title: 'Attention Is All You Need',
        authors: ['Ashish Vaswani', 'Noam Shazeer'],
        abstract: null,
        published_date: null,
        url: 'https://arxiv.org/abs/abc.123',
        pdf_url: null,
        citation_count: 0,
        metadata: {},
        library_match: { paper_id: 42, has_project_links: false, zotero_item_key: null },
      },
    ],
    total: 1,
    per_source_counts: { arxiv: 1 },
    degraded_sources: [],
    source_errors: {},
  };
}

function renderPalette() {
  return render(
    <MemoryRouter initialEntries={['/feed']}>
      <Routes>
        <Route path="/feed" element={<CommandPaletteSearch />} />
        <Route path="/paper/:paperId" element={<div>Paper detail page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('CommandPaletteSearch (F1)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.clearAllMocks();
    useCommandPalette.getState()._reset();
    mockSearchPreview.mockResolvedValue(makeResponse());
  });

  afterEach(() => {
    vi.useRealTimers();
    useCommandPalette.getState()._reset();
  });

  it('opens via the global ⌘K keydown registered by the hook', () => {
    renderPalette();
    expect(useCommandPalette.getState().isOpen).toBe(false);

    fireEvent.keyDown(window, { key: 'k', metaKey: true });

    expect(useCommandPalette.getState().isOpen).toBe(true);
  });

  it('closes on Escape when open', () => {
    renderPalette();
    act(() => useCommandPalette.getState().open());
    expect(useCommandPalette.getState().isOpen).toBe(true);

    fireEvent.keyDown(window, { key: 'Escape' });

    expect(useCommandPalette.getState().isOpen).toBe(false);
  });

  it('runs the debounced search and navigates to /paper/:id on select', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPalette();

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your papers/i);
    await user.type(input, 'attention');

    // Flush the 250ms debounce.
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    await waitFor(() => expect(mockSearchPreview).toHaveBeenCalledWith('attention'));

    const result = await screen.findByText('Attention Is All You Need');
    await user.click(result);

    await waitFor(() => expect(screen.getByText('Paper detail page')).toBeInTheDocument());
    expect(useCommandPalette.getState().isOpen).toBe(false);
  });

  it('shows a friendly error state when the search fails', async () => {
    mockSearchPreview.mockRejectedValueOnce(new Error('network down'));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPalette();

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your papers/i);
    await user.type(input, 'broken');

    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    expect(await screen.findByText(/couldn't search right now/i)).toBeInTheDocument();
  });
});
