/**
 * SettingsRail — §-grouped left navigation for the 2-pane Settings IA.
 *
 * Renders Roman-numeral section groups with nested rail items. The active item
 * is highlighted. Dynamic §II Sources items come from useQuery(['sources']).
 *
 * RBAC: §IV System section is hidden entirely for non-admin users.
 *       §II Sources rail items are admin-only (sourced from /api/sources which
 *       is admin-gated on the backend; we also hide on the frontend).
 */
import { useQuery } from '@tanstack/react-query';
import { fetchSources } from '@/lib/api';
import { cn } from '@/lib/utils';
import { SOURCE_DISPLAY_NAMES } from './SourceSection';
import type { SourceConfig } from '@/types';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RailSection {
  /** Roman numeral label shown as section header (e.g. "§I"). */
  label: string;
  /** Display title next to the numeral. */
  title: string;
  /** admin-only — entire section hidden for non-admin. */
  adminOnly?: boolean;
  items: RailItem[];
}

export interface RailItem {
  /** URL-safe slug used in `?section=&item=` routing. */
  section: string;
  item: string;
  label: string;
  /** Small status dot colour — 'ok' (green), 'warn' (yellow), undefined = none. */
  status?: 'ok' | 'warn';
}

// ---------------------------------------------------------------------------
// Static section definitions (§I, §III-§VI; §II is dynamic)
// ---------------------------------------------------------------------------

export const STATIC_SECTIONS: RailSection[] = [
  {
    label: '§I',
    title: 'Account',
    items: [
      { section: 'account', item: 'profile', label: 'Profile & Email' },
      { section: 'account', item: 'appearance', label: 'Appearance' },
    ],
  },
  // §II Sources — injected dynamically from useQuery(['sources'])
  {
    label: '§III',
    title: 'Models',
    adminOnly: true,
    items: [
      { section: 'models', item: 'llm', label: 'LLM Models' },
      { section: 'models', item: 'providers', label: 'Cloud Providers' },
    ],
  },
  {
    label: '§IV',
    title: 'System',
    adminOnly: true,
    items: [
      { section: 'system', item: 'automation', label: 'Automation' },
      { section: 'system', item: 'extraction', label: 'Extraction Templates' },
      { section: 'system', item: 'pulse', label: 'Pulse' },
      { section: 'system', item: 'timer', label: 'Timer' },
      { section: 'system', item: 'observability', label: 'Observability' },
    ],
  },
  {
    label: '§V',
    title: 'Integrations',
    items: [
      { section: 'integrations', item: 'telegram', label: 'Telegram' },
      { section: 'integrations', item: 'zotero', label: 'Zotero' },
    ],
  },
  {
    label: '§VI',
    title: 'Research',
    items: [
      { section: 'research', item: 'topics', label: 'Topics' },
      { section: 'research', item: 'authors', label: 'Authors' },
      { section: 'research', item: 'spaced-repetition', label: 'Spaced Repetition' },
    ],
  },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface SettingsRailProps {
  activeSection: string;
  activeItem: string;
  isAdmin: boolean;
  onSelect: (section: string, item: string) => void;
}

export function SettingsRail({ activeSection, activeItem, isAdmin, onSelect }: SettingsRailProps) {
  const { data: sources = [] } = useQuery<SourceConfig[]>({
    queryKey: ['sources'],
    queryFn: fetchSources,
    staleTime: 30_000,
    // Only fetch for admins — non-admins don't see §II Sources
    enabled: isAdmin,
  });

  // Build §II Sources section from live data
  const sourcesSection: RailSection = {
    label: '§II',
    title: 'Sources',
    adminOnly: true,
    items: sources.map((s) => ({
      section: 'sources',
      item: s.source_type,
      label: SOURCE_DISPLAY_NAMES[s.source_type] ?? s.source_type,
      status: s.enabled ? ('ok' as const) : undefined,
    })),
  };

  // Splice §II after §I
  const sections: RailSection[] = [
    STATIC_SECTIONS[0]!, // §I Account
    sourcesSection,      // §II Sources (dynamic)
    ...STATIC_SECTIONS.slice(1), // §III-§VI
  ];

  // Filter out admin-only sections for non-admin users
  const visibleSections = sections.filter((s) => !s.adminOnly || isAdmin);

  return (
    <nav
      aria-label="Settings navigation"
      className="shrink-0 border-r border-hair bg-[hsl(var(--surface-1))] lg:w-60 py-4 overflow-y-auto"
    >
      {visibleSections.map((section) => (
        <div key={section.label} className="mb-4">
          {/* Section header */}
          <div className="flex items-baseline gap-1.5 px-4 mb-1">
            <span className="font-mono text-[10px] text-muted-foreground">{section.label}</span>
            <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              {section.title}
            </span>
          </div>

          {/* Items */}
          {section.items.map((item) => {
            const isActive = activeSection === item.section && activeItem === item.item;
            return (
              <button
                key={`${item.section}-${item.item}`}
                type="button"
                aria-current={isActive ? 'page' : undefined}
                className={cn(
                  'w-full flex items-center gap-2 px-5 py-1.5 text-sm text-left transition-colors',
                  isActive
                    ? 'bg-[hsl(var(--ring)_/_0.12)] text-strong font-medium'
                    : 'text-muted-foreground hover:text-foreground hover:bg-[hsl(var(--muted)_/_0.5)]',
                )}
                onClick={() => onSelect(item.section, item.item)}
              >
                {/* Source enabled/disabled dot */}
                {item.status !== undefined && (
                  <span
                    aria-hidden
                    className={cn(
                      'h-1.5 w-1.5 rounded-full shrink-0',
                      item.status === 'ok' ? 'bg-[hsl(var(--status-ok))]' : 'bg-[hsl(var(--muted-foreground))]',
                    )}
                  />
                )}
                {item.label}
              </button>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
