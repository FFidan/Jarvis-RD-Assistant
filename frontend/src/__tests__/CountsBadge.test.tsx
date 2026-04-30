import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CountsBadge } from '@/components/feed/CountsBadge';
import { useFeedCounts } from '@/lib/api';

vi.mock('@/lib/api', () => ({
  useFeedCounts: vi.fn(),
}));

describe('CountsBadge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing while data is loading (isLoading=true)', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as ReturnType<typeof useFeedCounts>);
    const { container } = render(<CountsBadge surface="inbox" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when data is undefined (not yet loaded)', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: undefined,
      isLoading: false,
    } as ReturnType<typeof useFeedCounts>);
    const { container } = render(<CountsBadge surface="library" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when count is 0 (zero is suppressed)', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 0, library: 0, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 0 },
      isLoading: false,
    } as ReturnType<typeof useFeedCounts>);
    const { container } = render(<CountsBadge surface="inbox" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders the count when count is a positive number', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 5, library: 0, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 5 },
      isLoading: false,
    } as ReturnType<typeof useFeedCounts>);
    render(<CountsBadge surface="inbox" />);
    expect(screen.getByText('5')).toBeInTheDocument();
  });

  it('renders the library count for surface="library"', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 0, library: 12, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 12 },
      isLoading: false,
    } as ReturnType<typeof useFeedCounts>);
    render(<CountsBadge surface="library" />);
    expect(screen.getByText('12')).toBeInTheDocument();
  });

  it('renders "999+" when count exceeds 999', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 1000, library: 0, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 1000 },
      isLoading: false,
    } as ReturnType<typeof useFeedCounts>);
    render(<CountsBadge surface="inbox" />);
    expect(screen.getByText('999+')).toBeInTheDocument();
    expect(screen.queryByText('1000')).not.toBeInTheDocument();
  });

  it('renders "999+" for count exactly 1000', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 0, library: 1000, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 1000 },
      isLoading: false,
    } as ReturnType<typeof useFeedCounts>);
    render(<CountsBadge surface="library" />);
    expect(screen.getByText('999+')).toBeInTheDocument();
  });

  it('renders the count as text for count exactly 999', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 999, library: 0, starred: 0, archived: 0, reading: 0, trash: 0, all_active: 999 },
      isLoading: false,
    } as ReturnType<typeof useFeedCounts>);
    render(<CountsBadge surface="inbox" />);
    expect(screen.getByText('999')).toBeInTheDocument();
    expect(screen.queryByText('999+')).not.toBeInTheDocument();
  });

  it('renders the trash count for surface="trash"', () => {
    vi.mocked(useFeedCounts).mockReturnValue({
      data: { inbox: 0, library: 0, starred: 0, archived: 0, reading: 0, trash: 3, all_active: 0 },
      isLoading: false,
    } as ReturnType<typeof useFeedCounts>);
    render(<CountsBadge surface="trash" />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });
});
