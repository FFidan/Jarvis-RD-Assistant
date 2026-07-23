import { useState } from 'react';
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
  Compass,
  WifiOff,
  SlidersHorizontal,
} from 'lucide-react';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import type { FeedCountsWithFacets, SurfaceView, LibraryFilter, InboxSourceFilter, FeedScope } from '@/types';
import { SOURCE_LABELS } from '@/lib/labels/sources';

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
  /** Active library/corpus scope — drives scope-honest facet copy (C-FACET-BE). */
  feedScope?: FeedScope;
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
// FacetListContent — shared between desktop rail and mobile drawer
// --------------------------------------------------------------------------

interface FacetListContentProps {
  counts: FeedCountsWithFacets | undefined;
  selection: FacetSelection;
  isOnline: boolean;
  feedScope: FeedScope;
  isStatusActive: (item: StatusItem) => boolean;
  handleStatusClick: (item: StatusItem) => void;
  handleDiscoverClick: () => void;
  handleSourceClick: (sourceType: string) => void;
  handleTopicClick: (topicId: number | 'untagged') => void;
  handleStarClick: () => void;
}

function FacetListContent({
  counts,
  selection,
  isOnline,
  feedScope,
  isStatusActive,
  handleStatusClick,
  handleDiscoverClick,
  handleSourceClick,
  handleTopicClick,
  handleStarClick,
}: FacetListContentProps) {
  const bySource = counts?.by_source ?? {};
  const byTopic = counts?.by_topic ?? [];
  const untagged = counts?.untagged ?? 0;
  const hasSourceFacets = Object.keys(bySource).length > 0;
  const hasTopicFacets = byTopic.length > 0 || untagged > 0;

  const starActive = selection.surface === 'library' && selection.filter === 'starred';
  const starCount = counts?.starred ?? 0;

  // Scope-honest copy for Source/Topic empty states.
  const isCorpus = feedScope === 'corpus';
  const sourceEmptyCopy = isCorpus
    ? 'No visible papers found for this source.'
    : 'No papers in your library yet — papers you save or that match your topics appear here.';
  const topicEmptyCopy = isCorpus
    ? 'No visible papers are tagged with a topic yet.'
    : 'No library papers tagged with a topic yet — add a topic in Settings and turn on "Auto-add matches".';

  return (
    <>
      {/* § Discover — visually primary block at the TOP of the rail */}
      <div
        className="mx-2 mb-2 rounded-md border border-primary/20 bg-primary/5 px-1 py-1"
        data-testid="facet-discover-block"
      >
        <FacetItem
          icon={<Compass size={14} />}
          label="Discover papers"
          active={selection.surface === 'search'}
          onClick={handleDiscoverClick}
          data-testid="facet-discover"
        />
      </div>

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
        <p className="px-3 py-1.5 text-xs text-muted-foreground/60" data-testid="facet-source-empty">
          {sourceEmptyCopy}
        </p>
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
        <p className="px-3 py-1.5 text-xs text-muted-foreground/60" data-testid="facet-topic-empty">
          {topicEmptyCopy}
        </p>
      ) : (
        <p className="px-3 py-1.5 text-xs text-muted-foreground/60 flex items-center gap-1">
          <WifiOff size={11} />
          Unavailable offline
        </p>
      )}
    </>
  );
}

// --------------------------------------------------------------------------
// FacetRail
// --------------------------------------------------------------------------

export function FacetRail({ counts, selection, onSelect, isOnline = true, feedScope = 'library' }: FacetRailProps) {
  const [sheetOpen, setSheetOpen] = useState(false);

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
    setSheetOpen(false);
  }

  function handleSourceClick(sourceType: string) {
    const alreadyActive = selection.sourceFacet === sourceType;
    onSelect({
      surface: selection.surface === 'trash' ? 'inbox' : selection.surface,
      sourceFacet: alreadyActive ? null : sourceType,
      topicFacet: null,
    });
    setSheetOpen(false);
  }

  function handleTopicClick(topicId: number | 'untagged') {
    const alreadyActive = selection.topicFacet === topicId;
    onSelect({
      topicFacet: alreadyActive ? null : topicId,
      sourceFacet: null,
    });
    setSheetOpen(false);
  }

  function handleDiscoverClick() {
    onSelect({ surface: 'search', filter: null, sourceFacet: null, topicFacet: null });
    setSheetOpen(false);
  }

  function handleStarClick() {
    const alreadyActive = selection.surface === 'library' && selection.filter === 'starred';
    if (alreadyActive) {
      onSelect({ surface: 'library', filter: null });
    } else {
      onSelect({ surface: 'library', filter: 'starred', sourceFacet: null, topicFacet: null });
    }
    setSheetOpen(false);
  }

  const contentProps = {
    counts,
    selection,
    isOnline,
    feedScope,
    isStatusActive,
    handleStatusClick,
    handleDiscoverClick,
    handleSourceClick,
    handleTopicClick,
    handleStarClick,
  };

  return (
    <>
      {/* Desktop rail — hidden on mobile */}
      <nav
        aria-label="Feed facets"
        className="hidden md:flex w-48 shrink-0 flex-col border-r border-hair bg-paper py-2"
        data-testid="facet-rail"
      >
        <FacetListContent {...contentProps} />
      </nav>

      {/* Mobile trigger + drawer — hidden on md+ */}
      <div className="md:hidden">
        <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
          <SheetTrigger asChild>
            <button
              className="flex items-center gap-1.5 rounded-md border border-hair bg-paper px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              data-testid="facet-mobile-trigger"
            >
              <SlidersHorizontal size={14} />
              Filters
            </button>
          </SheetTrigger>
          <SheetContent side="left" className="w-64 p-0">
            <SheetHeader className="px-4 pt-4 pb-2">
              <SheetTitle className="text-sm font-semibold uppercase tracking-widest text-muted-foreground">
                Filters
              </SheetTitle>
            </SheetHeader>
            <div className="flex flex-col overflow-y-auto py-1">
              <FacetListContent {...contentProps} />
            </div>
          </SheetContent>
        </Sheet>
      </div>
    </>
  );
}
