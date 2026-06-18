/**
 * SettingsRail — §-grouped left navigation for the 2-pane Settings IA.
 *
 * Renders Roman-numeral section groups with nested rail items. The active item
 * is highlighted.
 *
 * RBAC: §IV System section is hidden entirely for non-admin users.
 *       §II Sources and §IV System items are admin-only.
 */
import { cn } from '@/lib/utils';

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
  /** admin-only — item hidden for non-admin users (section may still be visible). */
  adminOnly?: boolean;
}

// ---------------------------------------------------------------------------
// All sections (static — §II Sources is now a single item, not dynamic)
// ---------------------------------------------------------------------------

export const ALL_SECTIONS: RailSection[] = [
  {
    label: '§I',
    title: 'Account',
    items: [
      { section: 'account', item: 'profile', label: 'Profile & Email' },
      { section: 'account', item: 'appearance', label: 'Appearance' },
    ],
  },
  {
    label: '§II',
    title: 'Sources',
    adminOnly: true,
    items: [
      { section: 'sources', item: 'sources', label: 'Sources' },
    ],
  },
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
      { section: 'system', item: 'smtp', label: 'Email / SMTP' },
      { section: 'system', item: 'pulse', label: 'Pulse' },
      { section: 'system', item: 'timer', label: 'Timer' },
      { section: 'system', item: 'observability', label: 'Observability' },
      { section: 'system', item: 'mode', label: 'Sign-in Method' },
    ],
  },
  {
    label: '§V',
    title: 'Integrations',
    items: [
      { section: 'integrations', item: 'telegram', label: 'Telegram' },
      { section: 'integrations', item: 'bot-token', label: 'Bot Token', adminOnly: true },
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
  // Filter out admin-only sections for non-admin users
  const visibleSections = ALL_SECTIONS.filter((s) => !s.adminOnly || isAdmin);

  return (
    <nav
      aria-label="Settings navigation"
      className="h-full w-full border-r border-hair bg-[hsl(var(--surface-1))] py-4 overflow-y-auto"
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

          {/* Items — also filter item-level adminOnly for non-admin users */}
          {section.items.filter((item) => !item.adminOnly || isAdmin).map((item) => {
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
