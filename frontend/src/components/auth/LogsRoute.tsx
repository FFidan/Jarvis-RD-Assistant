import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth-store';

interface LogsRouteProps {
  children: ReactNode;
}

/**
 * Route guard for /logs.
 *
 * Accepts admin users (magic-link sessions with role === 'admin') and legacy
 * api-key sessions where a raw API key is held in the store (user === null but
 * apiKey !== null). Both session types are authorised by the backend for the
 * /logs endpoints. AdminOnlyRoute is intentionally NOT reused here because it
 * also guards /admin/* which must remain stricter (admin role required).
 */
export function LogsRoute({ children }: LogsRouteProps) {
  const user = useAuthStore((s) => s.user);
  const apiKey = useAuthStore((s) => s.apiKey);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const allowed = user?.role === 'admin' || (user === null && isAuthenticated && apiKey !== null);

  if (!allowed) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
