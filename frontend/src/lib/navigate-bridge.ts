import type { NavigateFunction } from 'react-router-dom';

let _navigate: NavigateFunction | null = null;

export function setNavigate(navigate: NavigateFunction): void {
  _navigate = navigate;
}

export function getNavigate(): NavigateFunction | null {
  return _navigate;
}
