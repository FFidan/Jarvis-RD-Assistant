import { Menu, Keyboard } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { HeaderPomodoro } from '@/components/layout/HeaderPomodoro';
import { JobsIndicator } from '@/components/layout/JobsIndicator';
import { ThemeToggle } from './ThemeToggle';
import { useKeyboardShortcuts } from '@/stores/keyboard-shortcuts-store';
import { BrandMark } from './BrandMark';
import { CommandPaletteSearch } from './CommandPaletteSearch';
import { UserAvatarMenu } from './UserAvatarMenu';
import { HeaderPill } from '@/components/logs/HeaderPill';

interface TopBarProps {
  onMenuClick?: () => void;
}

export function TopBar({ onMenuClick }: TopBarProps) {
  const openShortcuts = useKeyboardShortcuts((s) => s.open);

  return (
    <header className="flex items-center gap-4 border-b border-hair bg-paper px-6 pt-[env(safe-area-inset-top)] h-[calc(3.5rem+env(safe-area-inset-top))]">
      {onMenuClick && (
        <Button variant="ghost" size="icon" onClick={onMenuClick} className="md:hidden" aria-label="Open menu">
          <Menu className="h-5 w-5" />
        </Button>
      )}
      <BrandMark />
      <div className="hidden md:flex flex-1 justify-center">
        <CommandPaletteSearch />
      </div>
      <div className="ml-auto md:ml-0 flex items-center gap-2 sm:gap-4">
        <HeaderPill />
        <JobsIndicator />
        <HeaderPomodoro />
        <Button
          variant="ghost"
          size="icon"
          onClick={openShortcuts}
          aria-label="Keyboard shortcuts"
          title="Keyboard shortcuts (?)"
          className="hidden sm:inline-flex"
        >
          <Keyboard className="h-5 w-5" />
        </Button>
        <ThemeToggle />
        <UserAvatarMenu />
      </div>
    </header>
  );
}
