import { useState, useEffect, type ReactNode } from 'react';
import { Sidebar } from '@/components/layout/Sidebar';
import { TopBar } from '@/components/layout/TopBar';
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet';
import { Toaster } from '@/components/ui/toaster';
import { usePomodoroTick } from '@/hooks/use-pomodoro-tick';
import { useJobStore, registerVisibilityHydrate } from '@/stores/job-store';

interface AppShellProps {
  children: ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const [mobileOpen, setMobileOpen] = useState(false);
  usePomodoroTick();

  // Re-subscribe to any jobs that were running before the page was refreshed
  const hydrate = useJobStore((s) => s.hydrate);
  useEffect(() => {
    hydrate();
  }, [hydrate]);

  // Re-hydrate job state when user returns to the tab after being away
  useEffect(() => {
    registerVisibilityHydrate();
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Desktop sidebar */}
      <div className="hidden md:block">
        <Sidebar />
      </div>

      {/* Mobile sidebar as sheet */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-64 p-0">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <Sidebar />
        </SheetContent>
      </Sheet>

      {/* Main content */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <TopBar onMenuClick={() => setMobileOpen(true)} />
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>

      <Toaster position="bottom-right" />
    </div>
  );
}
