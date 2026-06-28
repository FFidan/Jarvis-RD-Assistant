/**
 * SettingsPage — 2-pane Settings IA (UI v3).
 *
 * Layout: `grid md:grid-cols-[240px_1fr]`
 *   Left: §-grouped SettingsRail (navigation)
 *   Right: SettingsDetailPane (breadcrumb + active section component)
 *
 * Mobile (<md): rail is hidden; a Sheet drawer opened via a menu button shows
 * the rail. Selecting an item closes the drawer. Detail pane is full-width.
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
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Menu } from 'lucide-react';
import { useAuthStore } from '@/stores/auth-store';
import { SettingsRail } from '@/components/settings/SettingsRail';
import { SettingsDetailPane } from '@/components/settings/SettingsDetailPane';
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet';

// ---------------------------------------------------------------------------
// RBAC section sets — mirrors old PERSONAL_TABS / SYSTEM_TABS
// ---------------------------------------------------------------------------

/** Sections accessible to every authenticated user. */
const PERSONAL_SECTIONS = new Set(['account', 'integrations', 'research']);

/** Sections visible to admin users only. */
const SYSTEM_SECTIONS = new Set(['sources', 'models', 'system']);

const ALL_VALID_SECTIONS = new Set([...PERSONAL_SECTIONS, ...SYSTEM_SECTIONS]);

// Default landing: Research → Topics
const DEFAULT_SECTION = 'research';
const DEFAULT_ITEM = 'topics';

// ---------------------------------------------------------------------------
// SettingsPage
// ---------------------------------------------------------------------------

export function SettingsPage() {
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === 'admin';

  const [mobileRailOpen, setMobileRailOpen] = useState(false);
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
    // Non-admin trying an admin-only item (e.g. bot-token) → redirect to default
    if (requestedSection === 'integrations' && requestedItem === 'bot-token' && !isAdmin) {
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

  const handleMobileSelect = (section: string, item: string) => {
    handleSelect(section, item);
    setMobileRailOpen(false);
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-6 pt-6 pb-4 shrink-0 flex items-center gap-3">
        {/* Mobile nav trigger — hidden on md+ where the rail is always visible */}
        <button
          type="button"
          aria-label="Open settings navigation"
          className="md:hidden shrink-0 rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          onClick={() => setMobileRailOpen(true)}
        >
          <Menu size={20} />
        </button>
        <h1 className="text-[32px] leading-tight tracking-tight text-strong">Settings</h1>
      </div>

      {/* Mobile rail Sheet drawer — hidden on md+ */}
      <Sheet open={mobileRailOpen} onOpenChange={setMobileRailOpen}>
        <SheetContent side="left" className="w-64 p-0">
          <SheetTitle className="sr-only">Settings navigation</SheetTitle>
          <SettingsRail
            activeSection={activeSection}
            activeItem={activeItem}
            isAdmin={isAdmin}
            onSelect={handleMobileSelect}
          />
        </SheetContent>
      </Sheet>

      {/* 2-pane grid — rail hidden on mobile (shown via Sheet above) */}
      <div className="flex flex-1 min-h-0 border-t border-hair md:grid md:grid-cols-[240px_1fr]">
        {/* Left rail — desktop only */}
        <div className="hidden md:block">
          <SettingsRail
            activeSection={activeSection}
            activeItem={activeItem}
            isAdmin={isAdmin}
            onSelect={handleSelect}
          />
        </div>

        {/* Right detail pane — full-width on mobile */}
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
