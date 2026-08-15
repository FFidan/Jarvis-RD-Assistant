/**
 * Sidebar — grouped roman-numeral navigation per the Shell/Sidebar+Admin IA spec.
 *
 * Density modes (device-scoped, `useNavPrefsStore`):
 *   - simple  — short essentials rail (My Day · Papers · Discover · Projects ·
 *               Ask · Learning Cards). The default until the researcher asks
 *               for more.
 *   - full    — the grouped layout below.
 *
 * Groups (full mode):
 *   Ⅰ Today     — My Day · Home · Pulse Deck · Papers (/feed?surface=library) · Discover
 *   Ⅱ Workspace — Projects · Ask · Extraction Table · Knowledge Graph · Citation Graph · Consensus
 *   Ⅲ Learn     — Learning Cards · Analytics
 *   Ⅳ Admin     — User Management · System Health · Audit Log · Backups · System Logs
 *                 (conditionally rendered for role === 'admin')
 *
 * Footer: nav-mode toggle · Settings link · HealthDots pill (navigates to
 *         /admin/system-health for admins; expands in-place for non-admins) ·
 *         Sign out button.
 */

import { Link, useLocation, useSearchParams } from 'react-router-dom';
import {
  Home,
  Sun,
  Sparkles,
  Newspaper,
  Search,
  MessageCircleQuestion,
  BarChart3,
  FolderKanban,
  GraduationCap,
  Settings,
  GitFork,
  Network,
  Scale,
  TableProperties,
  ScrollText,
  ShieldCheck,
  Users,
  Activity,
  Database,
  ChevronLeft,
  ChevronRight,
  Sliders,
  LogOut,
  X,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useUIStore } from '@/stores/ui-store';
import { useAuthStore } from '@/stores/auth-store';
import { useNavPrefsStore } from '@/stores/nav-prefs-store';
import { useResearchMilestoneStore } from '@/stores/research-milestone-store';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { BrandMark } from '@/components/layout/BrandMark';
import { HealthDots } from '@/components/shared/HealthDots';

// ---------------------------------------------------------------------------
// Nav data
// ---------------------------------------------------------------------------

interface NavItem {
  path: string;
  label: string;
  icon: LucideIcon;
  testid?: string;
  tourId?: string;
}

interface NavGroup {
  numeral: string;
  label: string;
  subLabel: string;
  items: NavItem[];
  adminOnly?: boolean;
}

const navGroups: NavGroup[] = [
  {
    numeral: 'Ⅰ',
    label: 'Today',
    subLabel: 'What needs your attention right now.',
    items: [
      { path: '/my-day', label: 'My Day', icon: Sun },
      { path: '/', label: 'Home', icon: Home },
      { path: '/pulse', label: 'Pulse Deck', icon: Sparkles },
      {
        path: '/feed?surface=library',
        label: 'Papers',
        icon: Newspaper,
        tourId: 'sidebar-library sidebar-analyze',
      },
      {
        path: '/feed?surface=search',
        label: 'Discover',
        icon: Search,
        testid: 'nav-discover',
        tourId: 'sidebar-discover',
      },
    ],
  },
  {
    numeral: 'Ⅱ',
    label: 'Workspace',
    subLabel: 'Projects, questions, and the tools that connect your papers.',
    items: [
      { path: '/projects', label: 'Projects', icon: FolderKanban },
      { path: '/ask', label: 'Ask', icon: MessageCircleQuestion, tourId: 'sidebar-ask' },
      { path: '/extractions', label: 'Extraction Table', icon: TableProperties },
      { path: '/knowledge', label: 'Knowledge Graph', icon: Network },
      { path: '/citations', label: 'Citation Graph', icon: GitFork },
      { path: '/consensus', label: 'Consensus', icon: Scale },
    ],
  },
  {
    numeral: 'Ⅲ',
    label: 'Learn',
    subLabel: 'Cards, analytics, and how your knowledge grows.',
    items: [
      { path: '/cards', label: 'Learning Cards', icon: GraduationCap },
      { path: '/analytics', label: 'Analytics', icon: BarChart3 },
    ],
  },
  {
    numeral: 'Ⅳ',
    label: 'Admin',
    subLabel: 'Users, health, and audit trail.',
    adminOnly: true,
    items: [
      { path: '/admin/users', label: 'User Management', icon: Users },
      { path: '/admin/system-health', label: 'System Health', icon: Activity },
      { path: '/admin/audit-log', label: 'Audit Log', icon: ShieldCheck },
      { path: '/admin/backups', label: 'Backups', icon: Database },
      { path: '/logs', label: 'System Logs', icon: ScrollText },
    ],
  },
];

// Simple-mode rail: the daily research loop. Pulse lives inside My Day and a
// landing dashboard is less useful day to day than the researcher's own
// projects, so Pulse Deck and Home stay in the full grouped view.
const SIMPLE_NAV_PATHS = new Set([
  '/my-day',
  '/feed?surface=library',
  '/feed?surface=search',
  '/projects',
  '/cards',
  '/ask',
]);

// What the "Show all features" toggle actually adds to the rail, read off the
// same set the rail is built from so the cue cannot drift out of step with it.
// Admin tools are excluded: they are not research features, and the cue is
// shown to every user regardless of role.
const ADVANCED_NAV_LABELS = navGroups
  .filter((group) => !group.adminOnly)
  .flatMap((group) => group.items)
  .filter((item) => !SIMPLE_NAV_PATHS.has(item.path))
  .map((item) => item.label);

function joinWithAnd(labels: string[]): string {
  const last = labels[labels.length - 1] ?? '';
  return labels.length > 1 ? `${labels.slice(0, -1).join(', ')}, and ${last}` : last;
}

const ADVANCED_NAV_SENTENCE = joinWithAnd(ADVANCED_NAV_LABELS);

// ---------------------------------------------------------------------------
// NavLink atom
// ---------------------------------------------------------------------------

interface NavLinkProps {
  item: NavItem;
  isActive: boolean;
  collapsed: boolean;
}

function NavLinkItem({ item, isActive, collapsed }: NavLinkProps) {
  const Icon = item.icon;
  const link = (
    <Link
      to={item.path}
      data-testid={item.testid}
      data-tour-id={item.tourId}
      className={cn(
        'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
        isActive
          ? 'bg-accent text-accent-foreground'
          : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
        collapsed && 'justify-center px-2',
      )}
      aria-current={isActive ? 'page' : undefined}
      aria-label={collapsed ? item.label : undefined}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      {!collapsed && <span>{item.label}</span>}
    </Link>
  );

  if (collapsed) {
    return (
      <Tooltip key={item.path}>
        <TooltipTrigger asChild>{link}</TooltipTrigger>
        <TooltipContent side="right">{item.label}</TooltipContent>
      </Tooltip>
    );
  }

  return link;
}

// ---------------------------------------------------------------------------
// Group header
// ---------------------------------------------------------------------------

interface GroupHeaderProps {
  numeral: string;
  label: string;
  subLabel: string;
  collapsed: boolean;
}

function GroupHeader({ numeral, label, subLabel, collapsed }: GroupHeaderProps) {
  if (collapsed) {
    // In collapsed mode, show a thin rule between groups
    return <div className="my-1 mx-2 border-t border-hair" aria-hidden="true" />;
  }

  return (
    <div className="px-3 pt-4 pb-1">
      <div className="flex items-baseline gap-2">
        <span className="text-[10px] font-mono font-semibold uppercase tracking-[0.18em] text-meta">
          {numeral}
        </span>
        <span className="text-[11px] font-semibold uppercase tracking-wider text-meta">
          {label}
        </span>
      </div>
      <p className="mt-0.5 text-[10px] italic text-meta leading-snug">
        {subLabel}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sidebar (exported)
// ---------------------------------------------------------------------------

function isNavItemActive(item: NavItem, pathname: string, searchParams: URLSearchParams): boolean {
  if (!item.path.includes('?')) {
    return pathname === item.path;
  }
  const [itemPathname, itemSearch] = item.path.split('?');
  if (pathname !== itemPathname) return false;
  // Query-aware /feed matching (R9): Discover owns surface=search; Library owns
  // every other /feed state (bare /feed, surface=library, surface=inbox) — Inbox
  // is Library's first tab, and bare /feed lands there before the redirect.
  const itemSurface = new URLSearchParams(itemSearch).get('surface');
  if (itemSurface !== null) {
    const currentSurface = searchParams.get('surface');
    return itemSurface === 'search'
      ? currentSurface === 'search'
      : currentSurface !== 'search';
  }
  const itemParams = new URLSearchParams(itemSearch);
  for (const [key, value] of itemParams) {
    if (searchParams.get(key) !== value) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Grouped (full) nav body
// ---------------------------------------------------------------------------

interface NavBodyProps {
  groups: NavGroup[];
  isAdmin: boolean;
  collapsed: boolean;
  pathname: string;
  searchParams: URLSearchParams;
}

function GroupedNav({ groups, isAdmin, collapsed, pathname, searchParams }: NavBodyProps) {
  return (
    <>
      {groups.map((group) => {
        // Admin-only groups are hidden for non-admin users
        if (group.adminOnly && !isAdmin) return null;

        const isAdminGroup = group.adminOnly;

        return (
          <div key={group.numeral} data-testid={`nav-group-${group.label.toLowerCase()}`}>
            <GroupHeader
              numeral={group.numeral}
              label={group.label}
              subLabel={group.subLabel}
              collapsed={collapsed}
            />
            <div className={cn('space-y-0.5', !collapsed && 'mt-1')}>
              {group.items.map((item) => (
                <NavLinkItem
                  key={item.path}
                  item={item}
                  isActive={isNavItemActive(item, pathname, searchParams)}
                  collapsed={collapsed}
                />
              ))}
            </div>
            {/* Extra spacing after admin group header to visually separate */}
            {isAdminGroup && !collapsed && <div className="pb-2" />}
          </div>
        );
      })}
    </>
  );
}

// ---------------------------------------------------------------------------
// Simple nav body — short essentials rail. The full grouped nav is one
// "Show all features" toggle away; there is no separate in-rail disclosure
// (it would duplicate the toggle and render the items ungrouped).
// ---------------------------------------------------------------------------

function SimpleNav({ groups, isAdmin, collapsed, pathname, searchParams }: NavBodyProps) {
  const essentials = groups
    .filter((g) => !g.adminOnly || isAdmin)
    .flatMap((g) => g.items)
    .filter((item) => SIMPLE_NAV_PATHS.has(item.path));

  return (
    <div className="space-y-0.5">
      {essentials.map((item) => (
        <NavLinkItem
          key={item.path}
          item={item}
          isActive={isNavItemActive(item, pathname, searchParams)}
          collapsed={collapsed}
        />
      ))}
    </div>
  );
}

interface SidebarProps {
  drawer?: boolean;
  onSearch?: () => void;
}

export function Sidebar({ drawer = false, onSearch }: SidebarProps) {
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const { sidebarCollapsed, toggleSidebar } = useUIStore();
  const { logout, user } = useAuthStore();
  const { navMode, toggleNavMode } = useNavPrefsStore();
  const completedMilestones = useResearchMilestoneStore((state) => state.completed);
  const advancedCueDismissed = useResearchMilestoneStore(
    (state) => state.advancedCueDismissed,
  );
  const dismissAdvancedCue = useResearchMilestoneStore(
    (state) => state.dismissAdvancedCue,
  );
  const isAdmin = user?.role === 'admin';
  const isSimple = navMode === 'simple';
  const hasResearchMilestone = completedMilestones.save || completedMilestones.analyze;
  const showAdvancedCue = isSimple && hasResearchMilestone && !advancedCueDismissed;
  const collapsed = drawer ? false : sidebarCollapsed;

  const handleNavModeToggle = () => {
    if (showAdvancedCue) dismissAdvancedCue();
    toggleNavMode();
  };

  return (
    <TooltipProvider delayDuration={0}>
      <div
        className={cn(
          'flex h-full flex-col border-r border-hair bg-paper transition-all duration-300',
          collapsed ? 'w-16' : 'w-64',
        )}
        data-testid="sidebar"
      >
        {/* Header */}
        <div className="flex h-14 items-center justify-between px-4">
          {!collapsed && <BrandMark />}
          {!drawer && (
            <Button
              variant="ghost"
              size="icon"
              onClick={toggleSidebar}
              className="ml-auto"
              aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            >
              {collapsed ? (
                <ChevronRight className="h-4 w-4" />
              ) : (
                <ChevronLeft className="h-4 w-4" />
              )}
            </Button>
          )}
        </div>

        <Separator />

        {drawer && onSearch && (
          <div className="p-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={onSearch}
              className="w-full justify-start"
            >
              <Search className="mr-2 h-4 w-4" aria-hidden="true" />
              Search your papers…
            </Button>
          </div>
        )}

        {/* Nav — full grouped layout, or the short simple-mode rail */}
        <nav className="flex-1 overflow-y-auto p-2" aria-label="Main navigation">
          {isSimple ? (
            <SimpleNav
              groups={navGroups}
              isAdmin={isAdmin}
              collapsed={collapsed}
              pathname={location.pathname}
              searchParams={searchParams}
            />
          ) : (
            <GroupedNav
              groups={navGroups}
              isAdmin={isAdmin}
              collapsed={collapsed}
              pathname={location.pathname}
              searchParams={searchParams}
            />
          )}
        </nav>

        <Separator />

        {/* Footer: Nav-mode toggle · Settings · HealthDots · Sign out */}
        <div className="p-3 space-y-2">
          {showAdvancedCue && !collapsed && (
            <div
              className="rounded-md border border-primary/30 bg-primary/5 p-3 text-xs text-muted-foreground"
              data-testid="advanced-workspace-cue"
              role="status"
            >
              <div className="flex items-start gap-2">
                <p className="leading-relaxed">
                  Ready for the next step? Show all features to add {ADVANCED_NAV_SENTENCE} to
                  the sidebar.
                </p>
                <Button
                  variant="ghost"
                  size="icon"
                  className="-mr-2 -mt-2 h-7 w-7 shrink-0"
                  onClick={dismissAdvancedCue}
                  aria-label="Dismiss workspace feature tip"
                >
                  <X className="h-3.5 w-3.5" aria-hidden="true" />
                </Button>
              </div>
            </div>
          )}

          {/* Nav-mode toggle — simple ⇄ full nav density (device-scoped) */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size={collapsed ? 'icon' : 'sm'}
                onClick={handleNavModeToggle}
                className="w-full"
                data-testid="nav-mode-toggle"
                aria-label={isSimple ? 'Show all features' : 'Simple view'}
              >
                <Sliders className="h-4 w-4 shrink-0" aria-hidden="true" />
                {!collapsed && (
                  <span className="ml-2">{isSimple ? 'Show all features' : 'Simple view'}</span>
                )}
              </Button>
            </TooltipTrigger>
            {collapsed && (
              <TooltipContent side="right">
                {isSimple ? 'Show all features' : 'Simple view'}
              </TooltipContent>
            )}
          </Tooltip>

          {/* Settings — footer utility link (not in any numbered group) */}
          {collapsed ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <Link
                  to="/settings"
                  data-tour-id="sidebar-settings"
                  className={cn(
                    'flex items-center justify-center rounded-md px-2 py-2 text-sm font-medium transition-colors text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                    location.pathname === '/settings' && 'bg-accent text-accent-foreground',
                  )}
                  aria-current={location.pathname === '/settings' ? 'page' : undefined}
                >
                  <Settings className="h-4 w-4 shrink-0" />
                </Link>
              </TooltipTrigger>
              <TooltipContent side="right">Settings</TooltipContent>
            </Tooltip>
          ) : (
            <Link
              to="/settings"
              data-tour-id="sidebar-settings"
              className={cn(
                'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                location.pathname === '/settings' && 'bg-accent text-accent-foreground',
              )}
              aria-current={location.pathname === '/settings' ? 'page' : undefined}
            >
              <Settings className="h-4 w-4 shrink-0" />
              <span>Settings</span>
            </Link>
          )}

          {/* HealthDots: admin users navigate to /admin/system-health; others expand in-place */}
          <HealthDots
            compact={collapsed}
            adminLink={isAdmin && !collapsed ? '/admin/system-health' : undefined}
          />

          {/* Build version — muted caption, hidden when collapsed (no room for text) */}
          {!collapsed && (
            <p
              className="px-2 text-[10px] text-muted-foreground text-center"
              data-testid="sidebar-app-version"
            >
              v{__APP_VERSION__}
            </p>
          )}

          {/* Sign out */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size={collapsed ? 'icon' : 'sm'}
                onClick={logout}
                className="w-full"
              >
                <LogOut className="h-4 w-4" />
                {!collapsed && <span className="ml-2">Sign out</span>}
              </Button>
            </TooltipTrigger>
            {collapsed && (
              <TooltipContent side="right">Sign out</TooltipContent>
            )}
          </Tooltip>
        </div>
      </div>
    </TooltipProvider>
  );
}
