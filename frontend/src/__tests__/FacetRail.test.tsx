/**
 * FacetRail component tests — F1 Feed IA Redesign
 *
 * Coverage:
 *  - §Status counts render for inbox/library/trash
 *  - §Star count renders and selection drives correct query params
 *  - §Source facet counts render and toggle correctly
 *  - §Topic facet counts render; untagged bucket shows
 *  - Trash appears as a §Status facet (not top-level tab)
 *  - FacetRail calls onSelect with correct partial selection
 *  - Active state is reflected via aria-pressed
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FacetRail } from '@/components/feed/FacetRail';
import type { FacetSelection } from '@/components/feed/FacetRail';
import type { FeedCountsWithFacets } from '@/types';

const EMPTY_COUNTS: FeedCountsWithFacets = {
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
  by_source: {},
  by_topic: [],
  untagged: 0,
};

const BASE_SELECTION: FacetSelection = {
  surface: 'inbox',
  filter: null,
  inboxSource: null,
  sourceFacet: null,
  topicFacet: null,
};

function makeRich(): FeedCountsWithFacets {
  return {
    ...EMPTY_COUNTS,
    inbox: 12,
    library: 45,
    reading_list: 8,
    reading: 3,
    done: 20,
    starred: 7,
    trash: 2,
    by_source: {
      arxiv: 25,
      semantic_scholar: 18,
      openalex: 5,
    },
    by_topic: [
      { topic_id: 1, name: 'Machine Learning', count: 30 },
      { topic_id: 2, name: 'Neuroscience', count: 10 },
    ],
    untagged: 5,
  };
}

describe('FacetRail', () => {
  const onSelect = vi.fn();

  beforeEach(() => {
    onSelect.mockClear();
  });

  it('renders the facet rail nav landmark', () => {
    render(
      <FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} />,
    );
    expect(screen.getByRole('navigation', { name: /feed facets/i })).toBeInTheDocument();
  });

  // ── §Status counts ────────────────────────────────────────────────────────

  it('renders §Status items with counts from FeedCountsWithFacets', () => {
    const counts = makeRich();
    render(<FacetRail counts={counts} selection={BASE_SELECTION} onSelect={onSelect} />);
    // Inbox count
    expect(screen.getByTestId('facet-status-inbox')).toHaveTextContent('12');
    // Reading-list count (maps to reading_list)
    expect(screen.getByTestId('facet-status-to_read')).toHaveTextContent('8');
    // Trash appears as status facet
    expect(screen.getByTestId('facet-status-trash')).toHaveTextContent('2');
  });

  it('marks inbox as active when surface=inbox', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} />);
    expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('facet-status-trash')).toHaveAttribute('aria-pressed', 'false');
  });

  it('marks trash as active when surface=trash', () => {
    render(
      <FacetRail
        counts={EMPTY_COUNTS}
        selection={{ ...BASE_SELECTION, surface: 'trash' }}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByTestId('facet-status-trash')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('facet-status-inbox')).toHaveAttribute('aria-pressed', 'false');
  });

  it('clicking Trash calls onSelect with surface=trash', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-status-trash'));
    expect(onSelect).toHaveBeenCalledOnce();
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.surface).toBe('trash');
    expect(call.filter).toBeNull();
  });

  it('clicking Reading calls onSelect with surface=library, filter=reading', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-status-reading'));
    expect(onSelect).toHaveBeenCalledOnce();
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.surface).toBe('library');
    expect(call.filter).toBe('reading');
  });

  // ── §Star ─────────────────────────────────────────────────────────────────

  it('renders §Star count from starred field', () => {
    const counts = makeRich();
    render(<FacetRail counts={counts} selection={BASE_SELECTION} onSelect={onSelect} />);
    expect(screen.getByTestId('facet-star-starred')).toHaveTextContent('7');
  });

  it('clicking Starred calls onSelect with surface=library, filter=starred', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-star-starred'));
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.surface).toBe('library');
    expect(call.filter).toBe('starred');
  });

  it('clicking Starred again (when active) clears filter', () => {
    const starredSelection: FacetSelection = { ...BASE_SELECTION, surface: 'library', filter: 'starred' };
    render(<FacetRail counts={EMPTY_COUNTS} selection={starredSelection} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-star-starred'));
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.filter).toBeNull();
  });

  // ── §Source ───────────────────────────────────────────────────────────────

  it('renders §Source facets from by_source with counts', () => {
    render(<FacetRail counts={makeRich()} selection={BASE_SELECTION} onSelect={onSelect} />);
    expect(screen.getByTestId('facet-source-arxiv')).toHaveTextContent('25');
    expect(screen.getByTestId('facet-source-semantic_scholar')).toHaveTextContent('18');
    expect(screen.getByTestId('facet-source-openalex')).toHaveTextContent('5');
  });

  it('clicking a source facet calls onSelect with that sourceFacet', () => {
    render(<FacetRail counts={makeRich()} selection={BASE_SELECTION} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-source-arxiv'));
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.sourceFacet).toBe('arxiv');
  });

  it('clicking an active source facet toggles it off', () => {
    const withSource: FacetSelection = { ...BASE_SELECTION, sourceFacet: 'arxiv' };
    render(<FacetRail counts={makeRich()} selection={withSource} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-source-arxiv'));
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.sourceFacet).toBeNull();
  });

  it('shows honest empty-source copy when by_source is empty and online', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} isOnline />);
    expect(screen.getByTestId('facet-source-empty')).toBeInTheDocument();
    expect(screen.getByTestId('facet-source-empty')).toHaveTextContent(
      'No papers in your library yet',
    );
  });

  it('shows honest empty-topic copy when by_topic is empty and online', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} isOnline />);
    expect(screen.getByTestId('facet-topic-empty')).toBeInTheDocument();
    expect(screen.getByTestId('facet-topic-empty')).toHaveTextContent(
      'No library papers tagged with a topic yet',
    );
  });

  it('shows offline indicator when isOnline=false', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} isOnline={false} />);
    // Should show "Unavailable offline" messages (source + topic)
    const offline = screen.getAllByText('Unavailable offline');
    expect(offline.length).toBeGreaterThanOrEqual(1);
  });

  // ── §Topic ────────────────────────────────────────────────────────────────

  it('renders §Topic facets from by_topic with counts', () => {
    render(<FacetRail counts={makeRich()} selection={BASE_SELECTION} onSelect={onSelect} />);
    expect(screen.getByTestId('facet-topic-1')).toHaveTextContent('Machine Learning');
    expect(screen.getByTestId('facet-topic-1')).toHaveTextContent('30');
    expect(screen.getByTestId('facet-topic-2')).toHaveTextContent('Neuroscience');
  });

  it('renders the Untagged bucket when untagged > 0', () => {
    render(<FacetRail counts={makeRich()} selection={BASE_SELECTION} onSelect={onSelect} />);
    expect(screen.getByTestId('facet-topic-untagged')).toHaveTextContent('Untagged');
    expect(screen.getByTestId('facet-topic-untagged')).toHaveTextContent('5');
  });

  it('clicking a topic facet calls onSelect with that topicFacet', () => {
    render(<FacetRail counts={makeRich()} selection={BASE_SELECTION} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-topic-1'));
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.topicFacet).toBe(1);
  });

  it('clicking Untagged calls onSelect with topicFacet="untagged"', () => {
    render(<FacetRail counts={makeRich()} selection={BASE_SELECTION} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-topic-untagged'));
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.topicFacet).toBe('untagged');
  });

  // ── Discover block at top ─────────────────────────────────────────────────

  it('renders the primary Discover block at the TOP of the rail (before Status)', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} />);
    const rail = screen.getByTestId('facet-rail');
    const discoverBlock = screen.getByTestId('facet-discover-block');
    const statusInbox = screen.getByTestId('facet-status-inbox');
    // Discover block must appear before any Status item in the DOM
    expect(rail.compareDocumentPosition(discoverBlock) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(
      discoverBlock.compareDocumentPosition(statusInbox) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it('clicking Discover calls onSelect with surface=search', () => {
    render(<FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId('facet-discover'));
    const call = onSelect.mock.calls[0]![0] as Partial<FacetSelection>;
    expect(call.surface).toBe('search');
  });

  it('marks Discover as active when surface=search', () => {
    render(
      <FacetRail
        counts={EMPTY_COUNTS}
        selection={{ ...BASE_SELECTION, surface: 'search' }}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByTestId('facet-discover')).toHaveAttribute('aria-pressed', 'true');
  });

  // ── Scope-honest facet copy ───────────────────────────────────────────────

  it('shows library-scoped empty copy when feedScope=library (default)', () => {
    render(
      <FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} isOnline feedScope="library" />,
    );
    expect(screen.getByTestId('facet-source-empty')).toHaveTextContent(
      'No papers in your library yet',
    );
    expect(screen.getByTestId('facet-topic-empty')).toHaveTextContent(
      'No library papers tagged with a topic yet',
    );
  });

  it('shows shared-library empty copy when feedScope=corpus — no "your library" wording', () => {
    render(
      <FacetRail counts={EMPTY_COUNTS} selection={BASE_SELECTION} onSelect={onSelect} isOnline feedScope="corpus" />,
    );
    const sourceEmpty = screen.getByTestId('facet-source-empty');
    const topicEmpty = screen.getByTestId('facet-topic-empty');
    // Must not mention "your library" in the shared-library scope
    expect(sourceEmpty.textContent).not.toMatch(/your library/i);
    expect(topicEmpty.textContent).not.toMatch(/your library/i);
    // Shared-library copy should mention the shared library
    expect(sourceEmpty.textContent).toMatch(/shared library/i);
    expect(topicEmpty.textContent).toMatch(/shared library/i);
  });

  // ── Mobile drawer ─────────────────────────────────────────────────────────

  it('renders the mobile Filters trigger button', () => {
    render(
      <FacetRail counts={makeRich()} selection={BASE_SELECTION} onSelect={onSelect} />,
    );
    // Mobile trigger button should be present in the DOM (CSS hides it at md+,
    // but JSDOM renders all content regardless of breakpoint classes)
    expect(screen.getByTestId('facet-mobile-trigger')).toBeInTheDocument();
  });
});
