import { Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  Home,
  Sun,
  Newspaper,
  BarChart3,
  FolderKanban,
  GraduationCap,
  Settings,
  GitFork,
  Network,
  TableProperties,
  ChevronLeft,
  ChevronRight,
  LogOut,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { checkHealth } from '@/lib/api';
import { useUIStore } from '@/stores/ui-store';
import { useAuthStore } from '@/stores/auth-store';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';

const navItems = [
  { path: '/', label: 'Home', icon: Home },
  { path: '/my-day', label: 'My Day', icon: Sun },
  { path: '/feed', label: 'Research Feed', icon: Newspaper },
  { path: '/analytics', label: 'Analytics', icon: BarChart3 },
  { path: '/projects', label: 'Projects', icon: FolderKanban },
  { path: '/cards', label: 'Learning Cards', icon: GraduationCap },
  { path: '/settings', label: 'Settings', icon: Settings },
  { path: '/citations', label: 'Citation Graph', icon: GitFork },
  { path: '/knowledge', label: 'Knowledge Graph', icon: Network },
  { path: '/extractions', label: 'Extraction Table', icon: TableProperties },
];

export function Sidebar() {
  const location = useLocation();
  const { sidebarCollapsed, toggleSidebar } = useUIStore();
  const { logout } = useAuthStore();

  const { data: paperIngestionHealthy } = useQuery({
    queryKey: ['health', 'paper_ingestion'],
    queryFn: () => checkHealth('/health/paper_ingestion'),
    refetchInterval: 30_000,
  });

  const { data: learningEngineHealthy } = useQuery({
    queryKey: ['health', 'learning_engine'],
    queryFn: () => checkHealth('/health/learning_engine'),
    refetchInterval: 30_000,
  });

  return (
    <TooltipProvider delayDuration={0}>
      <div
        className={cn(
          'flex h-full flex-col border-r bg-card transition-all duration-300',
          sidebarCollapsed ? 'w-16' : 'w-64',
        )}
      >
        {/* Header */}
        <div className="flex h-14 items-center justify-between px-4">
          {!sidebarCollapsed && (
            <span className="text-lg font-bold">JARVIS</span>
          )}
          <Button variant="ghost" size="icon" onClick={toggleSidebar} className="ml-auto">
            {sidebarCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
          </Button>
        </div>

        <Separator />

        {/* Nav */}
        <nav className="flex-1 space-y-1 overflow-y-auto p-2">
          {navItems.map((item) => {
            const isActive = location.pathname === item.path;
            const Icon = item.icon;
            const link = (
              <Link
                key={item.path}
                to={item.path}
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
          {!sidebarCollapsed && (
            <div className="space-y-1 text-xs text-muted-foreground">
              <div className="flex items-center gap-2">
                <span className={cn('h-2 w-2 rounded-full', paperIngestionHealthy ? 'bg-green-500' : 'bg-red-500')} />
                <span>Paper Ingestion</span>
              </div>
              <div className="flex items-center gap-2">
                <span className={cn('h-2 w-2 rounded-full', learningEngineHealthy ? 'bg-green-500' : 'bg-red-500')} />
                <span>Learning Engine</span>
              </div>
            </div>
          )}
          {sidebarCollapsed && (
            <div className="flex justify-center gap-1">
              <span className={cn('h-2 w-2 rounded-full', paperIngestionHealthy ? 'bg-green-500' : 'bg-red-500')} />
              <span className={cn('h-2 w-2 rounded-full', learningEngineHealthy ? 'bg-green-500' : 'bg-red-500')} />
            </div>
          )}
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
