import { useLocation } from 'react-router-dom';
import { Menu } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { HeaderPomodoro } from '@/components/layout/HeaderPomodoro';
import { JobsIndicator } from '@/components/layout/JobsIndicator';

const pageTitles: Record<string, string> = {
  '/': 'Home',
  '/my-day': 'My Day',
  '/feed': 'Research Feed',
  '/analytics': 'Analytics',
  '/projects': 'Projects',
  '/cards': 'Learning Cards',
  '/settings': 'Settings',
  '/citations': 'Citation Graph',
  '/knowledge': 'Knowledge Graph',
  '/extractions': 'Extraction Table',
};

function getPageTitle(pathname: string): string {
  if (pageTitles[pathname]) return pageTitles[pathname];
  if (pathname.startsWith('/paper/')) return 'Paper Detail';
  return 'JARVIS';
}

interface TopBarProps {
  onMenuClick?: () => void;
}

export function TopBar({ onMenuClick }: TopBarProps) {
  const location = useLocation();
  const title = getPageTitle(location.pathname);

  return (
    <header className="flex h-14 items-center gap-4 border-b bg-card px-6">
      {onMenuClick && (
        <Button variant="ghost" size="icon" onClick={onMenuClick} className="md:hidden">
          <Menu className="h-5 w-5" />
        </Button>
      )}
      <h1 className="text-lg font-semibold">{title}</h1>
      <div className="ml-auto flex items-center gap-2">
        <JobsIndicator />
        <HeaderPomodoro />
      </div>
    </header>
  );
}
