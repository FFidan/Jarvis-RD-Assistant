import { useState, useEffect, lazy, Suspense, type ReactNode } from 'react';
import { Sidebar } from '@/components/layout/Sidebar';
import { TopBar } from '@/components/layout/TopBar';
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet';
import { Toaster } from '@/components/ui/toaster';
import { usePomodoroTick } from '@/hooks/use-pomodoro-tick';
import { useThemeEffect } from '@/hooks/use-theme-effect';
import { useAppearance } from '@/hooks/use-appearance';
import { useJobStore, registerVisibilityHydrate } from '@/stores/job-store';
import { KeyboardCheatSheet } from '@/components/shared/KeyboardCheatSheet';
import { ConnectivityBanner } from '@/components/layout/ConnectivityBanner';
import { MaintenanceBanner } from '@/components/shared/MaintenanceBanner';
import { useCommandPalette } from '@/stores/command-palette-store';

// Keep the tour dependency chunk out of the eager bundle. The tour is only
// shown to first-time users.
const OnboardingTour = lazy(
  () => import('@/components/onboarding/OnboardingTour'),
);

interface AppShellProps {
  children: ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const openCommandPalette = useCommandPalette((state) => state.open);
  usePomodoroTick();
  useThemeEffect();
  useAppearance();

  // Re-subscribe to any jobs that were running before the page was refreshed
  const hydrate = useJobStore((s) => s.hydrate);
  useEffect(() => {
    hydrate();
  }, [hydrate]);

  // Re-hydrate job state when user returns to the tab after being away.
  // Return the cleanup so the listener is removed on unmount.
  useEffect(() => {
    return registerVisibilityHydrate();
  }, []);

  const handleMobileSearch = () => {
    setMobileOpen(false);
    openCommandPalette();
  };

  return (
    <div className="flex h-[100dvh] overflow-hidden">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[100] focus:rounded-md focus:bg-paper focus:px-4 focus:py-2 focus:text-foreground focus:shadow-lg focus:outline-none focus:ring-2 focus:ring-ring"
      >
        Skip to main content
      </a>

      {/* Desktop sidebar */}
      <div className="hidden md:block">
        <Sidebar />
      </div>

      {/* Mobile sidebar as sheet */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-64 p-0" style={{ paddingLeft: 'env(safe-area-inset-left)' }}>
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <Sidebar drawer onSearch={handleMobileSearch} />
        </SheetContent>
      </Sheet>

      {/* Main content */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <MaintenanceBanner />
        <TopBar onMenuClick={() => setMobileOpen(true)} />
        <ConnectivityBanner />
        <main
          id="main-content"
          tabIndex={-1}
          className="flex-1 overflow-y-auto bg-paper p-6 pb-[calc(1.5rem+env(safe-area-inset-bottom))]"
        >
          {children}
        </main>
      </div>

      <Toaster position="bottom-right" toastOptions={{ style: { paddingBottom: 'env(safe-area-inset-bottom)' } }} />
      <KeyboardCheatSheet />
      <Suspense fallback={null}>
        <OnboardingTour />
      </Suspense>
    </div>
  );
}
