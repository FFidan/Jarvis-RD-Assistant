import { useEffect } from 'react';
import { applyAppearance, loadAppearance } from '@/lib/theme';

/** Called once in AppShell — reads saved prefs and applies CSS classes to <html>. */
export function useAppearance() {
  useEffect(() => {
    applyAppearance(loadAppearance());
  }, []);
}
