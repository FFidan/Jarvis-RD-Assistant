import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CountsBadge } from '@/components/feed/CountsBadge';

vi.mock('@tanstack/react-query', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-query')>();
  return {
    ...actual,
    useQuery: vi.fn(),
  };
});

import { useQuery } from '@tanstack/react-query';

const EMPTY_COUNTS = { inbox: 0, library: 0, reading_list: 0, reading: 0, done: 0, starred: 0, trash: 0, active: 0, kept: 0, all_non_trash: 0 };

describe('CountsBadge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing while data is loading (isLoading=true)', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as ReturnType<typeof useQuery>);
    const { container } = render(<CountsBadge surface="inbox" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when data is undefined (not yet loaded)', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: undefined,
      isLoading: false,
    } as ReturnType<typeof useQuery>);
    const { container } = render(<CountsBadge surface="library" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when count is 0 (zero is suppressed)', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: EMPTY_COUNTS,
      isLoading: false,
    } as ReturnType<typeof useQuery>);
    const { container } = render(<CountsBadge surface="inbox" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders the count when count is a positive number', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: { ...EMPTY_COUNTS, inbox: 5, active: 5, all_non_trash: 5 },
      isLoading: false,
    } as ReturnType<typeof useQuery>);
    render(<CountsBadge surface="inbox" />);
    expect(screen.getByText('5')).toBeInTheDocument();
  });

  it('renders the library count for surface="library"', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: { ...EMPTY_COUNTS, library: 12, all_non_trash: 12 },
      isLoading: false,
    } as ReturnType<typeof useQuery>);
    render(<CountsBadge surface="library" />);
    expect(screen.getByText('12')).toBeInTheDocument();
  });

  it('renders "999+" when count exceeds 999', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: { ...EMPTY_COUNTS, inbox: 1000, active: 1000, all_non_trash: 1000 },
      isLoading: false,
    } as ReturnType<typeof useQuery>);
    render(<CountsBadge surface="inbox" />);
    expect(screen.getByText('999+')).toBeInTheDocument();
    expect(screen.queryByText('1000')).not.toBeInTheDocument();
  });

  it('renders "999+" for count exactly 1000', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: { ...EMPTY_COUNTS, library: 1000, all_non_trash: 1000 },
      isLoading: false,
    } as ReturnType<typeof useQuery>);
    render(<CountsBadge surface="library" />);
    expect(screen.getByText('999+')).toBeInTheDocument();
  });

  it('renders the count as text for count exactly 999', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: { ...EMPTY_COUNTS, inbox: 999, active: 999, all_non_trash: 999 },
      isLoading: false,
    } as ReturnType<typeof useQuery>);
    render(<CountsBadge surface="inbox" />);
    expect(screen.getByText('999')).toBeInTheDocument();
    expect(screen.queryByText('999+')).not.toBeInTheDocument();
  });

  it('renders the trash count for surface="trash"', () => {
    vi.mocked(useQuery).mockReturnValue({
      data: { ...EMPTY_COUNTS, trash: 3 },
      isLoading: false,
    } as ReturnType<typeof useQuery>);
    render(<CountsBadge surface="trash" />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });
});
