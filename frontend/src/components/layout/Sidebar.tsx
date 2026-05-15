import { Link, useLocation } from 'react-router-dom';
import {
  Home,
  Sun,
  Sparkles,
  Newspaper,
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
  ChevronLeft,
  ChevronRight,
  LogOut,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useUIStore } from '@/stores/ui-store';
import { useAuthStore } from '@/stores/auth-store';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { BrandMark } from '@/components/layout/BrandMark';
import { HealthDots } from '@/components/shared/HealthDots';

const navItems = [
  { path: '/', label: 'Home', icon: Home },
  { path: '/my-day', label: 'My Day', icon: Sun },
  { path: '/pulse', label: 'Pulse Deck', icon: Sparkles },
  { path: '/feed', label: 'Research Feed', icon: Newspaper },
  { path: '/ask', label: 'Ask', icon: MessageCircleQuestion },
  { path: '/analytics', label: 'Analytics', icon: BarChart3 },
  { path: '/projects', label: 'Projects', icon: FolderKanban },
  { path: '/cards', label: 'Learning Cards', icon: GraduationCap },
  { path: '/settings', label: 'Settings', icon: Settings },
  { path: '/citations', label: 'Citation Graph', icon: GitFork },
  { path: '/knowledge', label: 'Knowledge Graph', icon: Network },
  { path: '/extractions', label: 'Extraction Table', icon: TableProperties },
];

/** Nav items that are only shown to users with role === 'admin'. */
const adminNavItems = [
  { path: '/logs', label: 'System Logs', icon: ScrollText },
  { path: '/admin/users', label: 'User Management', icon: Users },
  { path: '/admin/audit-log', label: 'Audit Log', icon: ShieldCheck },
];

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
      >
        {/* Header */}
        <div className="flex h-14 items-center justify-between px-4">
          {!sidebarCollapsed && (
            <BrandMark />
          )}
          <Button variant="ghost" size="icon" onClick={toggleSidebar} className="ml-auto" aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
            {sidebarCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
          </Button>
        </div>

        <Separator />

        {/* Nav */}
        <nav className="flex-1 space-y-1 overflow-y-auto p-2">
          {[...navItems, ...(isAdmin ? adminNavItems : [])].map((item) => {
            const isActive = location.pathname === item.path;
            const Icon = item.icon;
            const link = (
              <Link
                key={item.path}
                to={item.path}
                data-tour-id={item.path === '/settings' ? 'sidebar-settings' : undefined}
                className={cn(
                  'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-accent text-accent-foreground'
                    : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                  sidebarCollapsed && 'justify-center px-2',
                )}
              >
                <Icon className="h-4 w-4 shrink-0" />
                {!sidebarCollapsed && <span>{item.label}</span>}
              </Link>
            );

            if (sidebarCollapsed) {
              return (
                <Tooltip key={item.path}>
                  <TooltipTrigger asChild>{link}</TooltipTrigger>
                  <TooltipContent side="right">{item.label}</TooltipContent>
                </Tooltip>
              );
            }

            return link;
          })}
        </nav>

        <Separator />

        {/* Footer: service health + logout */}
        <div className="p-3 space-y-2">
          <HealthDots compact={sidebarCollapsed} />
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size={sidebarCollapsed ? 'icon' : 'sm'} onClick={logout} className="w-full">
                <LogOut className="h-4 w-4" />
                {!sidebarCollapsed && <span className="ml-2">Logout</span>}
              </Button>
            </TooltipTrigger>
            {sidebarCollapsed && <TooltipContent side="right">Logout</TooltipContent>}
          </Tooltip>
        </div>
      </div>
    </TooltipProvider>
  );
}
