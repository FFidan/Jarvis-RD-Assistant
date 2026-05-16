import { cn } from '@/lib/utils';
import {
  Inbox,
  BookOpen,
  Library,
  CheckCircle,
  Trash2,
  Star,
  FileText,
  Tag,
  Wifi,
  WifiOff,
} from 'lucide-react';
import type { FeedCountsWithFacets, SurfaceView, LibraryFilter, InboxSourceFilter } from '@/types';

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

export type FacetGroup =
  | { kind: 'status'; value: StatusFacetKey }
  | { kind: 'star' }
  | { kind: 'source'; value: string }
  | { kind: 'topic'; value: number | 'untagged' }
  | { kind: 'search' };

export type StatusFacetKey = SurfaceView | 'reading' | 'to_read' | 'done';

export interface FacetSelection {
  /** Top-level surface ('inbox' | 'library' | 'trash' | 'search') */
  surface: SurfaceView;
  /** Library sub-filter when surface=library */
  filter: LibraryFilter | null;
  /** Inbox source chip when surface=inbox */
  inboxSource: InboxSourceFilter | null;
  /** Active §Source facet (source_type string) */
  sourceFacet: string | null;
  /** Active §Topic facet (topic_id number or 'untagged') */
  topicFacet: number | 'untagged' | null;
}

interface FacetRailProps {
  counts: FeedCountsWithFacets | undefined;
  selection: FacetSelection;
  onSelect: (next: Partial<FacetSelection>) => void;
  isOnline?: boolean;
}

// --------------------------------------------------------------------------
// Config
// --------------------------------------------------------------------------

interface StatusItem {
  key: SurfaceView | LibraryFilter;
  label: string;
  icon: React.ReactNode;
  countsKey: keyof Pick<
    FeedCountsWithFacets,
    'inbox' | 'library' | 'reading_list' | 'reading' | 'done' | 'starred' | 'trash'
  >;
  /** Drives surface / filter when clicked */
  surface: SurfaceView;
  filter: LibraryFilter | null;
}

const STATUS_ITEMS: StatusItem[] = [
  {
    key: 'inbox',
    label: 'Inbox',
    icon: <Inbox size={14} />,
    countsKey: 'inbox',
    surface: 'inbox',
    filter: null,
  },
  {
    // "Library" §Status = all library papers (surface=library, no filter)
    // Provides `data-testid="facet-status-library"` for IA navigation and tests.
    key: 'library',
    label: 'Library',
    icon: <Library size={14} />,
    countsKey: 'library',
    surface: 'library',
    filter: null,
  },
  {
    key: 'reading',
    label: 'Reading',
    icon: <BookOpen size={14} />,
    countsKey: 'reading',
    surface: 'library',
    filter: 'reading',
  },
  {
    key: 'to_read',
    label: 'Reading List',
    icon: <Library size={14} />,
    countsKey: 'reading_list',
    surface: 'library',
    filter: 'to_read',
  },
  {
    key: 'done',
    label: 'Done',
    icon: <CheckCircle size={14} />,
    countsKey: 'done',
    surface: 'library',
    filter: 'done',
  },
  {
    key: 'trash',
    label: 'Trash',
    icon: <Trash2 size={14} />,
    countsKey: 'trash',
    surface: 'trash',
    filter: null,
  },
];

const SOURCE_LABELS: Record<string, string> = {
  arxiv: 'arXiv',
  semantic_scholar: 'Semantic Scholar',
  openalex: 'OpenAlex',
  pubmed: 'PubMed',
  local: 'Local PDF',
};

// --------------------------------------------------------------------------
// Sub-components
// --------------------------------------------------------------------------

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <p className="px-3 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/60 select-none">
      {children}
    </p>
  );
}

interface FacetItemProps {
  icon?: React.ReactNode;
  label: string;
  count?: number | null;
  active: boolean;
  onClick: () => void;
  'data-testid'?: string;
}

function FacetItem({ icon, label, count, active, onClick, ...rest }: FacetItemProps) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      data-testid={rest['data-testid']}
      className={cn(
        'group flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-sm transition-colors',
        active
          ? 'bg-accent text-accent-foreground font-medium'
          : 'text-muted-foreground hover:bg-muted hover:text-foreground',
      )}
    >
      {icon && <span className="shrink-0 opacity-70 group-hover:opacity-100">{icon}</span>}
      <span className="min-w-0 flex-1 truncate text-left">{label}</span>
      {count != null && count > 0 && (
        <span
          className={cn(
            'ml-auto shrink-0 rounded-full px-1.5 py-0.5 text-[10px] tabular-nums leading-none',
            active ? 'bg-accent-foreground/10 text-accent-foreground' : 'bg-muted text-muted-foreground',
          )}
        >
          {count > 999 ? '999+' : count}
        </span>
      )}
    </button>
  );
}

function OnlineOnlyNotice() {
  return (
    <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-muted-foreground/60 bg-muted/50">
      <WifiOff size={10} />
      online only
    </span>
  );
}

// --------------------------------------------------------------------------
// FacetRail
// --------------------------------------------------------------------------

export function FacetRail({ counts, selection, onSelect, isOnline = true }: FacetRailProps) {
  // Derive active status item for §Status section
  function isStatusActive(item: StatusItem): boolean {
    if (item.surface !== selection.surface) return false;
    if (item.surface === 'library') return selection.filter === item.filter;
    return true;
  }

  function handleStatusClick(item: StatusItem) {
    onSelect({
      surface: item.surface,
      filter: item.filter,
      sourceFacet: null,
      topicFacet: null,
      inboxSource: null,
    });
  }

  function handleSourceClick(sourceType: string) {
    const alreadyActive = selection.sourceFacet === sourceType;
    onSelect({
      surface: selection.surface === 'trash' ? 'inbox' : selection.surface,
      sourceFacet: alreadyActive ? null : sourceType,
      topicFacet: null,
    });
  }

  function handleTopicClick(topicId: number | 'untagged') {
    const alreadyActive = selection.topicFacet === topicId;
    onSelect({
      topicFacet: alreadyActive ? null : topicId,
      sourceFacet: null,
    });
  }

  function handleStarClick() {
    const alreadyActive = selection.surface === 'library' && selection.filter === 'starred';
    if (alreadyActive) {
      onSelect({ surface: 'library', filter: null });
    } else {
      onSelect({ surface: 'library', filter: 'starred', sourceFacet: null, topicFacet: null });
    }
  }

  const bySource = counts?.by_source ?? {};
  const byTopic = counts?.by_topic ?? [];
  const untagged = counts?.untagged ?? 0;
  const hasSourceFacets = Object.keys(bySource).length > 0;
  const hasTopicFacets = byTopic.length > 0 || untagged > 0;

  const starActive = selection.surface === 'library' && selection.filter === 'starred';
  const starCount = counts?.starred ?? 0;

  return (
    <nav
      aria-label="Feed facets"
      className="flex w-48 shrink-0 flex-col border-r border-hair bg-paper py-2"
      data-testid="facet-rail"
    >
      {/* § Status */}
      <SectionHeader>Status</SectionHeader>
      {STATUS_ITEMS.map((item) => (
        <FacetItem
          key={item.key}
          icon={item.icon}
          label={item.label}
          count={counts?.[item.countsKey]}
          active={isStatusActive(item)}
          onClick={() => handleStatusClick(item)}
          data-testid={`facet-status-${item.key}`}
        />
      ))}

      {/* § Star */}
      <SectionHeader>Star</SectionHeader>
      <FacetItem
        icon={<Star size={14} />}
        label="Starred"
        count={starCount}
        active={starActive}
        onClick={handleStarClick}
        data-testid="facet-star-starred"
      />

      {/* § Source — online-only */}
      <SectionHeader>
        <span className="flex items-center gap-1">
          Source
          {!isOnline && <OnlineOnlyNotice />}
        </span>
      </SectionHeader>
      {isOnline && hasSourceFacets ? (
        Object.entries(bySource).map(([sourceType, count]) => (
          <FacetItem
            key={sourceType}
            icon={<FileText size={14} />}
            label={SOURCE_LABELS[sourceType] ?? sourceType}
            count={count}
            active={selection.sourceFacet === sourceType}
            onClick={() => handleSourceClick(sourceType)}
            data-testid={`facet-source-${sourceType}`}
          />
        ))
      ) : isOnline ? (
        <p className="px-3 py-1.5 text-xs text-muted-foreground/60">No papers yet</p>
      ) : (
        <p className="px-3 py-1.5 text-xs text-muted-foreground/60 flex items-center gap-1">
          <WifiOff size={11} />
          Unavailable offline
        </p>
      )}

      {/* § Topic — online-only */}
      <SectionHeader>
        <span className="flex items-center gap-1">
          Topic
          {!isOnline && <OnlineOnlyNotice />}
        </span>
      </SectionHeader>
      {isOnline && hasTopicFacets ? (
        <>
          {byTopic.map(({ topic_id, name, count }) => (
            <FacetItem
              key={topic_id}
              icon={<Tag size={14} />}
              label={name}
              count={count}
              active={selection.topicFacet === topic_id}
              onClick={() => handleTopicClick(topic_id)}
              data-testid={`facet-topic-${topic_id}`}
            />
          ))}
          {untagged > 0 && (
            <FacetItem
              icon={<Tag size={14} className="opacity-40" />}
              label="Untagged"
              count={untagged}
              active={selection.topicFacet === 'untagged'}
              onClick={() => handleTopicClick('untagged')}
              data-testid="facet-topic-untagged"
            />
          )}
        </>
      ) : isOnline ? (
        <p className="px-3 py-1.5 text-xs text-muted-foreground/60">No topics configured</p>
      ) : (
        <p className="px-3 py-1.5 text-xs text-muted-foreground/60 flex items-center gap-1">
          <WifiOff size={11} />
          Unavailable offline
        </p>
      )}

      {/* § Discovery / Search — go to search surface */}
      <div className="mt-auto border-t border-hair pt-2">
        <FacetItem
          icon={<Wifi size={14} />}
          label="Discover"
          active={selection.surface === 'search'}
          onClick={() =>
            onSelect({ surface: 'search', filter: null, sourceFacet: null, topicFacet: null })
          }
          data-testid="facet-discover"
        />
      </div>
    </nav>
  );
}
