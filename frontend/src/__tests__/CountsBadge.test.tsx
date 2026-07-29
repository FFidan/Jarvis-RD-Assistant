import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CountsBadge } from '@/components/feed/CountsBadge';
import type { FeedCountsResponse } from '@/types';

vi.mock('@tanstack/react-query', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-query')>();
  return {
    ...actual,
    useQuery: vi.fn(),
  };
});

import { useQuery } from '@tanstack/react-query';

const EMPTY_COUNTS: FeedCountsResponse = {
  inbox: 0,
  library: 0,
  reading_list: 0,
  reading: 0,
  done: 0,
  starred: 0,
  trash: 0,
  active: 0,
  kept: 0,
  all_non_trash: 0,
};

type CountsSurface = keyof FeedCountsResponse;

type HiddenBadgeCase = readonly [
  name: string,
  surface: CountsSurface,
  data: FeedCountsResponse | undefined,
  isLoading: boolean,
];

type VisibleBadgeCase = readonly [
  name: string,
  surface: CountsSurface,
  data: FeedCountsResponse,
  expected: string,
  unexpected?: string,
];

function counts(overrides: Partial<FeedCountsResponse>): FeedCountsResponse {
  return { ...EMPTY_COUNTS, ...overrides };
}

const HIDDEN_BADGE_CASES = [
  ['renders nothing while data is loading', 'inbox', undefined, true],
  ['renders nothing when data is undefined', 'library', undefined, false],
  ['renders nothing when the count is zero', 'inbox', EMPTY_COUNTS, false],
] satisfies ReadonlyArray<HiddenBadgeCase>;

const VISIBLE_BADGE_CASES = [
  ['renders a positive inbox count', 'inbox', counts({ inbox: 5 }), '5', undefined],
  ['renders the library count', 'library', counts({ library: 12 }), '12', undefined],
  ['caps counts above 999', 'inbox', counts({ inbox: 1000 }), '999+', '1000'],
  ['caps a count of exactly 1000', 'library', counts({ library: 1000 }), '999+', undefined],
  ['renders exactly 999 without a suffix', 'inbox', counts({ inbox: 999 }), '999', '999+'],
  ['renders the trash count', 'trash', counts({ trash: 3 }), '3', undefined],
] satisfies ReadonlyArray<VisibleBadgeCase>;

function mockCountsQuery(
  data: FeedCountsResponse | undefined,
  isLoading: boolean,
): void {
  vi.mocked(useQuery).mockReturnValue({
    data,
    isLoading,
  } as ReturnType<typeof useQuery>);
}

describe('CountsBadge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it.each(HIDDEN_BADGE_CASES)('%s', (_name, surface, data, isLoading) => {
    mockCountsQuery(data, isLoading);
    const { container } = render(<CountsBadge surface={surface} />);
    expect(container.firstChild).toBeNull();
  });

  it.each(VISIBLE_BADGE_CASES)(
    '%s',
    (_name, surface, data, expected, unexpected) => {
      mockCountsQuery(data, false);
      render(<CountsBadge surface={surface} />);
      expect(screen.getByText(expected)).toBeInTheDocument();
      if (unexpected !== undefined) {
        expect(screen.queryByText(unexpected)).not.toBeInTheDocument();
      }
    },
  );
});
