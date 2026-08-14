/**
 * CommandPaletteSearch — global ⌘K command palette tests
 *
 * Coverage:
 *  - ⌘K (the global keydown registered by the controller hook) opens the
 *    palette via the store.
 *  - Esc closes it.
 *  - Typing a query runs the debounced library search and renders results
 *    (title + authors).
 *  - Selecting a result navigates to /paper/:id and closes the palette.
 *  - External discovery is a separate, labelled action into Discover — never
 *    mixed into the library results.
 *  - A failed search shows the friendly error state (no throw).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { CommandPaletteSearch } from '@/components/layout/CommandPaletteSearch';
import { useCommandPalette } from '@/stores/command-palette-store';
import { fetchFeedPapers } from '@/lib/api';
import type { FeedResponse } from '@/types';
import { makeFeedPaper } from '@/__tests__/fixtures/feed-paper';

vi.mock('@/lib/api', () => ({
  fetchFeedPapers: vi.fn(),
}));

const mockFetchFeedPapers = vi.mocked(fetchFeedPapers);

function makeResponse(): FeedResponse {
  return {
    papers: [
      makeFeedPaper({
        id: 42,
        external_id: 'abc.123',
        title: 'Attention Is All You Need',
        authors: ['Ashish Vaswani', 'Noam Shazeer'],
        url: 'https://arxiv.org/abs/abc.123',
      }),
    ],
    total: 1,
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

describe('CommandPaletteSearch', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.clearAllMocks();
    useCommandPalette.getState()._reset();
    mockFetchFeedPapers.mockResolvedValue(makeResponse());
  });

  afterEach(() => {
    vi.useRealTimers();
    useCommandPalette.getState()._reset();
  });

  it('opens via the global ⌘K keydown registered by the hook', () => {
    renderPalette();
    expect(
      screen.getByRole('button', { name: /Search your library/ }),
    ).toBeInTheDocument();
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

    const input = await screen.findByPlaceholderText(/search your library…/i);
    await user.type(input, 'attention');

    // Flush the 250ms debounce.
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // The box says it searches YOUR library, so it must hit the paper feed —
    // not the external-source preview.
    await waitFor(() =>
      expect(mockFetchFeedPapers).toHaveBeenCalledWith({ q: 'attention', limit: 8 }),
    );

    const result = await screen.findByText('Attention Is All You Need');
    await user.click(result);

    await waitFor(() => expect(screen.getByText('Paper detail page')).toBeInTheDocument());
    expect(useCommandPalette.getState().isOpen).toBe(false);
  });

  it('shows a friendly error state when the search fails', async () => {
    mockFetchFeedPapers.mockRejectedValueOnce(new Error('network down'));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPalette();

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your library/i);
    await user.type(input, 'broken');

    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    expect(await screen.findByText(/couldn't search right now/i)).toBeInTheDocument();
  });

  it('fails into the error state after 8 s timeout instead of hanging', async () => {
    // The feed request never resolves — simulates a slow backend.
    mockFetchFeedPapers.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPalette();

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your library/i);
    await user.type(input, 'slow');

    // Advance past debounce (250 ms) — search is now in-flight.
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // Still loading — has not yet timed out.
    expect(useCommandPalette.getState().loading).toBe(true);
    expect(useCommandPalette.getState().errored).toBe(false);

    // Advance past the 8 000 ms search timeout.
    await act(async () => {
      vi.advanceTimersByTime(8_000);
    });

    expect(useCommandPalette.getState().loading).toBe(false);
    expect(useCommandPalette.getState().errored).toBe(true);
    expect(await screen.findByText(/couldn't search right now/i)).toBeInTheDocument();
  });

  it('offers external discovery as a separate labelled action into Discover', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const LocationProbe = () => {
      const loc = useLocation();
      return <div data-testid="loc">{loc.pathname + loc.search}</div>;
    };

    render(
      <MemoryRouter initialEntries={['/feed']}>
        <LocationProbe />
        <Routes>
          <Route path="/feed" element={<CommandPaletteSearch />} />
        </Routes>
      </MemoryRouter>,
    );

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your library/i);
    await user.type(input, 'residual');

    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    const discover = await screen.findByText(/Search external sources for/i);
    expect(discover).toHaveTextContent('residual');

    await user.click(discover);

    await waitFor(() => expect(useCommandPalette.getState().isOpen).toBe(false));
    await waitFor(() =>
      expect(screen.getByTestId('loc').textContent).toBe('/feed?surface=search&q=residual'),
    );
  });

  it('never labels a library result as missing from the library', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPalette();

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your library/i);
    await user.type(input, 'attention');

    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    await screen.findByText('Attention Is All You Need');
    // Every hit comes from the caller's own papers, so the old
    // "Not in your library yet" caption can never be true here.
    expect(screen.queryByText(/Not in your library yet/i)).not.toBeInTheDocument();
  });

  it('reports an empty result set as no library matches', async () => {
    mockFetchFeedPapers.mockResolvedValue({ papers: [], total: 0 });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPalette();

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your library/i);
    await user.type(input, 'nothing');

    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    expect(await screen.findByText(/No matches in your library/i)).toBeInTheDocument();
  });

  it('renders an accessible description for the dialog (a11y)', async () => {
    renderPalette();
    act(() => useCommandPalette.getState().open());

    // The dialog must have an accessible description so screen readers
    // announce the palette's purpose. DialogDescription provides this.
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAccessibleDescription();
  });
});
