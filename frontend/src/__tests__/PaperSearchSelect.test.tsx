import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PaperSearchSelect } from '@/components/shared/PaperSearchSelect';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  const papers = [
    { id: 1, title: 'Attention Is All You Need' },
    { id: 2, title: 'BERT Paper' },
    { id: 3, title: 'GPT-4 Technical Report' },
  ];
  return {
    ...orig,
    fetchPapersBrief: vi.fn().mockResolvedValue(papers),
    searchPapersBrief: vi.fn().mockResolvedValue([papers[0]]),
  };
});

const defaultPapers = [
  { id: 1, title: 'Attention Is All You Need' },
  { id: 2, title: 'BERT Paper' },
  { id: 3, title: 'GPT-4 Technical Report' },
];

function renderComponent(props: Partial<React.ComponentProps<typeof PaperSearchSelect>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PaperSearchSelect {...props} />
    </QueryClientProvider>,
  );
}

describe('PaperSearchSelect', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    // Restore default mocks after tests that override them
    const { fetchPapersBrief, searchPapersBrief } = await import('@/lib/api');
    vi.mocked(fetchPapersBrief).mockResolvedValue(defaultPapers);
    const firstPaper = defaultPapers[0];
    if (!firstPaper) throw new Error('test fixture: defaultPapers is empty');
    vi.mocked(searchPapersBrief).mockResolvedValue([firstPaper]);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders default placeholder "Search papers by title..."', () => {
    renderComponent();
    expect(screen.getByPlaceholderText('Search papers by title...')).toBeInTheDocument();
  });

  it('shows dropdown with paper titles on focus', async () => {
    const user = userEvent.setup();
    renderComponent();
    const input = screen.getByPlaceholderText('Search papers by title...');
    await user.click(input);
    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
      expect(screen.getByText('BERT Paper')).toBeInTheDocument();
      expect(screen.getByText('GPT-4 Technical Report')).toBeInTheDocument();
    });
    const { fetchPapersBrief } = await import('@/lib/api');
    expect(fetchPapersBrief).toHaveBeenCalled();
  });

  it('shows "No papers found" when API returns empty array', async () => {
    const { fetchPapersBrief } = await import('@/lib/api');
    vi.mocked(fetchPapersBrief).mockResolvedValue([]);
    const user = userEvent.setup();
    renderComponent();
    const input = screen.getByPlaceholderText('Search papers by title...');
    await user.click(input);
    await waitFor(() => {
      expect(screen.getByText('No papers found')).toBeInTheDocument();
    });
  });

  it('debounces search — calls searchPapersBrief only after 300ms', async () => {
    vi.useFakeTimers();
    const { searchPapersBrief } = await import('@/lib/api');

    renderComponent();
    const input = screen.getByPlaceholderText('Search papers by title...');

    // Focus the input to open dropdown
    await act(async () => {
      input.focus();
    });

    // Clear any initial calls from the first render
    vi.mocked(searchPapersBrief).mockClear();

    // Type "Att" using fireEvent (userEvent doesn't work well with fake timers)
    await act(async () => {
      fireEvent.change(input, { target: { value: 'Att' } });
    });

    // Advance 299ms — debounce should not have fired yet
    await act(async () => {
      vi.advanceTimersByTime(299);
    });
    expect(searchPapersBrief).not.toHaveBeenCalled();

    // Advance to 300ms total — debounce fires and triggers query
    await act(async () => {
      vi.advanceTimersByTime(1);
    });

    // The debounced state update triggers a re-render with the search query
    // Since debouncedSearch length >= 2, searchPapersBrief is called
    expect(searchPapersBrief).toHaveBeenCalledWith('Att');
  });

  it('single-select: click paper calls onChange and closes dropdown', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    renderComponent({ onChange });

    const input = screen.getByPlaceholderText('Search papers by title...');
    await user.click(input);

    await waitFor(() => {
      expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Attention Is All You Need'));
    expect(onChange).toHaveBeenCalledWith(1);

    // Dropdown should close after selection
    await waitFor(() => {
      expect(screen.queryByText('BERT Paper')).not.toBeInTheDocument();
    });
  });

  it('single-select: clear button calls onChange(null)', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    renderComponent({ value: 1, onChange });

    // The clear button (X) should be visible when a value is set
    const clearButtons = screen.getAllByRole('button');
    // Find the clear/X button — it's the ghost variant button with absolute positioning
    const clearButton = clearButtons.find(
      (btn) => btn.className.includes('absolute'),
    );
    expect(clearButton).toBeDefined();
    await user.click(clearButton!);
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('multi-select: renders badges for selected papers', async () => {
    const onChangeMulti = vi.fn();
    renderComponent({ values: [1, 2], onChangeMulti });

    await waitFor(() => {
      // Badges show truncated paper titles or fallback "Paper {id}"
      const badges = screen.getAllByText(/Attention Is All You Need|BERT Paper|Paper 1|Paper 2/);
      expect(badges.length).toBeGreaterThanOrEqual(2);
    });
  });

  it('multi-select: badge X removes paper — onChangeMulti([2]) when removing id=1', async () => {
    const onChangeMulti = vi.fn();
    const user = userEvent.setup();
    renderComponent({ values: [1, 2], onChangeMulti });

    await waitFor(() => {
      // Wait for papers to load and badges to show real titles
      expect(screen.getByText(/Attention Is All You Need/)).toBeInTheDocument();
    });

    // Find badge remove buttons — they are plain <button> elements inside Badge
    const badgeButtons = screen.getAllByRole('button');
    // Badge X buttons are the ones without "absolute" class (those are the input clear buttons)
    const badgeXButtons = badgeButtons.filter(
      (btn) => !btn.className.includes('absolute') && !btn.className.includes('pl-8'),
    );
    // Click the first badge's X to remove paper 1
    const firstBadgeXBtn = badgeXButtons[0];
    if (!firstBadgeXBtn) throw new Error('test fixture: badge X button not found');
    await user.click(firstBadgeXBtn);
    expect(onChangeMulti).toHaveBeenCalledWith([2]);
  });
});
