/**
 * SettingsPage — 2-pane Settings IA (UI v3).
 *
 * Layout: `grid lg:grid-cols-[240px_1fr]`
 *   Left: §-grouped SettingsRail (navigation)
 *   Right: SettingsDetailPane (breadcrumb + active section component)
 *
 * URL routing: `?section=<slug>&item=<slug>`
 *   Omitting both params defaults to section=research, item=topics
 *   (preserves existing default tab=topics behaviour).
 *
 * RBAC — preserved exactly from the old tab-bar model:
 *   PERSONAL_SECTIONS: accessible to all authenticated users.
 *   SYSTEM_SECTIONS:   accessible to admin users only.
 *   Non-admin deep-linking to a system section → redirect to default personal item.
 */
import { useSearchParams } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth-store';
import { SettingsRail } from '@/components/settings/SettingsRail';
import { SettingsDetailPane } from '@/components/settings/SettingsDetailPane';

// ---------------------------------------------------------------------------
// RBAC section sets — mirrors old PERSONAL_TABS / SYSTEM_TABS
// ---------------------------------------------------------------------------

/** Sections accessible to every authenticated user. */
const PERSONAL_SECTIONS = new Set(['account', 'integrations', 'research']);

/** Sections visible to admin users only. */
const SYSTEM_SECTIONS = new Set(['sources', 'models', 'system']);

const ALL_VALID_SECTIONS = new Set([...PERSONAL_SECTIONS, ...SYSTEM_SECTIONS]);

// Default landing per spec §3.5: §VI Research → Topics
const DEFAULT_SECTION = 'research';
const DEFAULT_ITEM = 'topics';

// ---------------------------------------------------------------------------
// SettingsPage
// ---------------------------------------------------------------------------

export function SettingsPage() {
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === 'admin';

  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSection = searchParams.get('section');
  const requestedItem = searchParams.get('item');

  // Resolve active section/item with RBAC redirect
  const { activeSection, activeItem } = (() => {
    // Unknown or missing section → default
    if (!requestedSection || !ALL_VALID_SECTIONS.has(requestedSection)) {
      return { activeSection: DEFAULT_SECTION, activeItem: DEFAULT_ITEM };
    }
    // Non-admin trying a system section → redirect to default personal
    if (SYSTEM_SECTIONS.has(requestedSection) && !isAdmin) {
      return { activeSection: DEFAULT_SECTION, activeItem: DEFAULT_ITEM };
    }
    // Valid section — use requested item or fall back to first reasonable default
    const item = requestedItem ?? getDefaultItem(requestedSection);
    return { activeSection: requestedSection, activeItem: item };
  })();

  const handleSelect = (section: string, item: string) => {
    const next = new URLSearchParams(searchParams);
    next.set('section', section);
    next.set('item', item);
    setSearchParams(next, { replace: true });
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-6 pt-6 pb-4 shrink-0">
        <h1 className="text-[32px] leading-tight tracking-tight text-strong">Settings</h1>
      </div>

      {/* 2-pane grid */}
      <div className="flex flex-1 min-h-0 border-t border-hair lg:grid lg:grid-cols-[240px_1fr]">
        {/* Left rail */}
        <SettingsRail
          activeSection={activeSection}
          activeItem={activeItem}
          isAdmin={isAdmin}
          onSelect={handleSelect}
        />

        {/* Right detail pane */}
        <SettingsDetailPane section={activeSection} item={activeItem} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Return a sensible default item for a section when none is specified. */
function getDefaultItem(section: string): string {
  switch (section) {
    case 'account':     return 'profile';
    case 'models':      return 'llm';
    case 'system':      return 'automation';
    case 'integrations': return 'telegram';
    case 'research':    return 'topics';
    case 'sources':     return ''; // dynamic — SettingsDetailPane handles empty
    default:            return '';
  }
}
