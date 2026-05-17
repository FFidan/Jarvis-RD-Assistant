/**
 * Sidebar — grouped roman-numeral navigation per the Shell/Sidebar+Admin IA spec.
 *
 * Groups:
 *   Ⅰ Today    — Home · My Day · Pulse Deck · Library · Discover
 *   Ⅱ Read     — Projects · Knowledge Graph · Citation Graph · Extraction Table
 *   Ⅲ Learn    — Learning Cards · Analytics
 *   Ⅳ Ask      — Ask
 *   Ⅴ Admin    — User Management · System Health · Audit Log · System Logs
 *               (conditionally rendered for role === 'admin')
 *
 * Footer: Settings link · HealthDots pill (navigates to /admin/system-health
 *         for admins; expands in-place for non-admins) · Logout button.
 */

import { Link, useLocation } from 'react-router-dom';
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
  TableProperties,
  ScrollText,
  ShieldCheck,
  Users,
  Activity,
  ChevronLeft,
  ChevronRight,
  LogOut,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useUIStore } from '@/stores/ui-store';
import { useAuthStore } from '@/stores/auth-store';
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
      { path: '/', label: 'Home', icon: Home },
      { path: '/my-day', label: 'My Day', icon: Sun },
      { path: '/pulse', label: 'Pulse Deck', icon: Sparkles },
      { path: '/feed', label: 'Library', icon: Newspaper },
      { path: '/feed?surface=search', label: 'Discover', icon: Search, testid: 'nav-discover' },
    ],
  },
  {
    numeral: 'Ⅱ',
    label: 'Read',
    subLabel: 'Your library, projects, and the graph that connects them.',
    items: [
      { path: '/projects', label: 'Projects', icon: FolderKanban },
      { path: '/knowledge', label: 'Knowledge Graph', icon: Network },
      { path: '/citations', label: 'Citation Graph', icon: GitFork },
      { path: '/extractions', label: 'Extraction Table', icon: TableProperties },
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
    label: 'Ask',
    subLabel: 'Cross-paper reasoning and workspace.',
    items: [
      { path: '/ask', label: 'Ask', icon: MessageCircleQuestion },
    ],
  },
  {
    numeral: 'Ⅴ',
    label: 'Admin',
    subLabel: 'Users, health, and audit trail.',
    adminOnly: true,
    items: [
      { path: '/admin/users', label: 'User Management', icon: Users },
      { path: '/admin/system-health', label: 'System Health', icon: Activity },
      { path: '/admin/audit-log', label: 'Audit Log', icon: ShieldCheck },
      { path: '/logs', label: 'System Logs', icon: ScrollText },
    ],
  },
];

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
      data-tour-id={item.path === '/settings' ? 'sidebar-settings' : undefined}
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
        <span className="text-[10px] font-mono font-semibold uppercase tracking-[0.18em] text-muted-foreground/60">
          {numeral}
        </span>
        <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/80">
          {label}
        </span>
      </div>
      <p className="mt-0.5 text-[10px] italic text-muted-foreground/50 leading-snug">
        {subLabel}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sidebar (exported)
// ---------------------------------------------------------------------------

export function Sidebar() {
  const location = useLocation();
  const { sidebarCollapsed, toggleSidebar } = useUIStore();
  const { logout, user } = useAuthStore();
  const isAdmin = user?.role === 'admin';

  return (
    <TooltipProvider delayDuration={0}>
      <div
        className={cn(
          'flex h-full flex-col border-r border-hair bg-paper transition-all duration-300',
          sidebarCollapsed ? 'w-16' : 'w-64',
        )}
        data-testid="sidebar"
      >
        {/* Header */}
        <div className="flex h-14 items-center justify-between px-4">
          {!sidebarCollapsed && <BrandMark />}
          <Button
            variant="ghost"
            size="icon"
            onClick={toggleSidebar}
            className="ml-auto"
            aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {sidebarCollapsed ? (
              <ChevronRight className="h-4 w-4" />
            ) : (
              <ChevronLeft className="h-4 w-4" />
            )}
          </Button>
        </div>

        <Separator />

        {/* Grouped Nav */}
        <nav className="flex-1 overflow-y-auto p-2" aria-label="Main navigation">
          {navGroups.map((group) => {
            // Admin-only groups are hidden for non-admin users
            if (group.adminOnly && !isAdmin) return null;

            const isAdminGroup = group.adminOnly;

            return (
              <div key={group.numeral} data-testid={`nav-group-${group.label.toLowerCase()}`}>
                <GroupHeader
                  numeral={group.numeral}
                  label={group.label}
                  subLabel={group.subLabel}
                  collapsed={sidebarCollapsed}
                />
                <div className={cn('space-y-0.5', !sidebarCollapsed && 'mt-1')}>
                  {group.items.map((item) => {
                    const isActive = location.pathname === item.path;
                    return (
                      <NavLinkItem
                        key={item.path}
                        item={item}
                        isActive={isActive}
                        collapsed={sidebarCollapsed}
                      />
                    );
                  })}
                </div>
                {/* Extra spacing after admin group header to visually separate */}
                {isAdminGroup && !sidebarCollapsed && (
                  <div className="pb-2" />
                )}
              </div>
            );
          })}
        </nav>

        <Separator />

        {/* Footer: Settings · HealthDots · Logout */}
        <div className="p-3 space-y-2">
          {/* Settings — footer utility link (not in any numbered group) */}
          {sidebarCollapsed ? (
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
            compact={sidebarCollapsed}
            adminLink={isAdmin && !sidebarCollapsed ? '/admin/system-health' : undefined}
          />

          {/* Logout */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size={sidebarCollapsed ? 'icon' : 'sm'}
                onClick={logout}
                className="w-full"
              >
                <LogOut className="h-4 w-4" />
                {!sidebarCollapsed && <span className="ml-2">Logout</span>}
              </Button>
            </TooltipTrigger>
            {sidebarCollapsed && (
              <TooltipContent side="right">Logout</TooltipContent>
            )}
          </Tooltip>
        </div>
      </div>
    </TooltipProvider>
  );
}
