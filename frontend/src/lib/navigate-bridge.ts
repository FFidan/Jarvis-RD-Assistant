import type { NavigateFunction } from 'react-router-dom';

// Singleton set once by App.tsx; consumed by job-store.ts (~line 238) to
// navigate on job completion outside of React's component tree.
let _navigate: NavigateFunction | null = null;

export function setNavigate(navigate: NavigateFunction): void {
  _navigate = navigate;
}

export function getNavigate(): NavigateFunction | null {
  return _navigate;
}
