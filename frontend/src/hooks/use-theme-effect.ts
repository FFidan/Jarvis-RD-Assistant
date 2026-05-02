import { useEffect, useState } from 'react';
import { useThemeStore, type Theme } from '@/stores/theme-store';

type Resolved = 'light' | 'dark';

function resolveTheme(theme: Theme): Resolved {
  if (theme === 'system') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  return theme;
}

/**
 * Subscribes to theme store + system color-scheme changes,
 * applies/removes `.dark` class on <html>, returns resolved theme.
 */
export function useThemeEffect(): { theme: Theme; resolvedTheme: Resolved } {
  const theme = useThemeStore((s) => s.theme);
  const [resolvedTheme, setResolvedTheme] = useState<Resolved>(() => resolveTheme(theme));

  useEffect(() => {
    const apply = () => {
      const resolved = resolveTheme(theme);
      setResolvedTheme(resolved);
      document.documentElement.classList.toggle('dark', resolved === 'dark');
    };
    apply();

    if (theme !== 'system') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', apply);
    return () => mq.removeEventListener('change', apply);
  }, [theme]);

  return { theme, resolvedTheme };
}
