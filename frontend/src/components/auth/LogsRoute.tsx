import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth-store';

interface LogsRouteProps {
  children: ReactNode;
}

/**
 * Route guard for /logs.
 *
 * Accepts authenticated admin users. API-key login mints the same owner session
 * cookie and user record as magic-link login, so there is no raw-key-only state.
 */
export function LogsRoute({ children }: LogsRouteProps) {
  const user = useAuthStore((s) => s.user);
  const allowed = user?.role === 'admin';

  if (!allowed) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
