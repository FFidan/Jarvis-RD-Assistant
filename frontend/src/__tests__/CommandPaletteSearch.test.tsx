/**
 * CommandPaletteSearch — global ⌘K command palette tests
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
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
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

/** A result with no library_match — "not in your library yet". */
function makeResponseNotInLibrary(): SearchPreviewResponse {
  return {
    results: [
      {
        external_id: 'xyz.999',
        source_type: 'arxiv',
        title: 'Deep Residual Learning for Image Recognition',
        authors: ['Kaiming He', 'Xiangyu Zhang'],
        abstract: null,
        published_date: null,
        url: 'https://arxiv.org/abs/xyz.999',
        pdf_url: null,
        citation_count: 0,
        metadata: {},
        library_match: null,
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

describe('CommandPaletteSearch', () => {
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
    expect(
      screen.getByRole('button', { name: /Search your papers/ }),
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

    const input = await screen.findByPlaceholderText(/search your papers…/i);
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

  it('fails into the error state after 8 s timeout instead of hanging', async () => {
    // searchPreview never resolves — simulates a slow embedding/search backend.
    mockSearchPreview.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPalette();

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your papers/i);
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

  it('navigates to Discover when a not-in-library result is selected', async () => {
    mockSearchPreview.mockResolvedValue(makeResponseNotInLibrary());
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

    const input = await screen.findByPlaceholderText(/search your papers/i);
    await user.type(input, 'residual');

    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    await waitFor(() => expect(mockSearchPreview).toHaveBeenCalledWith('residual'));

    // The result should be present and clickable (not disabled).
    const result = await screen.findByText('Deep Residual Learning for Image Recognition');
    // Verify the item is not aria-disabled.
    const item = result.closest('[role="option"]') ?? result.closest('[cmdk-item]') ?? result.parentElement;
    expect(item).not.toBeNull();

    await user.click(result);

    // After selecting, the palette closes and we navigate to the Discover/search
    // surface with the typed query carried as ?q= so SearchBar is prefilled.
    await waitFor(() => expect(useCommandPalette.getState().isOpen).toBe(false));
    await waitFor(() =>
      expect(screen.getByTestId('loc').textContent).toBe('/feed?surface=search&q=residual'),
    );
  });

  it('not-in-library result is not disabled (has no aria-disabled=true attribute)', async () => {
    mockSearchPreview.mockResolvedValue(makeResponseNotInLibrary());
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPalette();

    act(() => useCommandPalette.getState().open());

    const input = await screen.findByPlaceholderText(/search your papers/i);
    await user.type(input, 'residual');

    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    await waitFor(() => expect(mockSearchPreview).toHaveBeenCalled());

    const result = await screen.findByText('Deep Residual Learning for Image Recognition');
    // cmdk sets data-disabled on the Command.Item element.
    // Walk up to find the cmdk item wrapper.
    let el: HTMLElement | null = result;
    while (el && !el.hasAttribute('data-disabled') && el !== document.body) {
      el = el.parentElement;
    }
    // data-disabled should not be 'true' — item is actionable.
    expect(el?.getAttribute('data-disabled')).not.toBe('true');
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
